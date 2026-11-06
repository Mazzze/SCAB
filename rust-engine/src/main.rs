//! Native OpenSCAD compilation scheduler. JSON-lines protocol on stdout.
//! The UI and source editor remain Qt; all geometry is still standard OpenSCAD.
use serde::{Deserialize,Serialize};
use serde_json::{json,Value};
use sha2::{Digest,Sha256};
use std::{collections::{HashMap,VecDeque},fs,io::{self,Read,Write},path::{Path,PathBuf},process::Command,sync::{Arc,Mutex,mpsc},thread,time::{Instant,Duration}};
use tempfile::Builder;

type Result<T> = std::result::Result<T,String>;
type Color = [f64;4];
const DEFAULT:Color=[0.38,0.75,0.73,1.];
#[derive(Deserialize)] struct Root {start:usize,end:usize,source:String}
#[derive(Deserialize)] struct Request {executable:String,directory:PathBuf,cache:PathBuf,frames:Vec<Vec<Root>>,#[serde(default="two")] workers:usize,#[serde(default)] draft:bool}
fn two()->usize{2}
#[derive(Clone,Debug)] struct Node {head:String,code:String,children:Vec<Node>}
impl Node {fn name(&self)->&str {self.head.split('(').next().unwrap_or("").trim().trim_start_matches(['!','#','%','*'])}}
#[derive(Clone)] struct Layer {color:Color,code:String}
#[derive(Serialize)] struct Chunk {start:usize,end:usize,solid:usize,parts:Vec<(Color,usize)>}
#[derive(Clone)] struct Task {key:String,code:String}
#[derive(Clone,Serialize)] struct MeshResult {path:Option<PathBuf>,cached:bool,log:String}
fn event(v:Value){println!("{}",v);let _=io::stdout().flush();}
fn err(e:impl std::fmt::Display)->String{e.to_string()}
fn command(exe:&str)->Command{
    let mut c=Command::new(exe);c.env("QT_QPA_PLATFORM","offscreen");
    #[cfg(windows)] {use std::os::windows::process::CommandExt;c.creation_flags(0x08000000);}
    c
}
fn parse(source:&str)->Result<Vec<Node>>{
    fn list(s:&str,p:&mut usize,close:bool)->Result<Vec<Node>> {
        let b=s.as_bytes();let mut out=Vec::new();
        loop {
            while *p<b.len() && b[*p].is_ascii_whitespace(){*p+=1;}
            if *p==b.len(){return if close {Err("Unclosed CSG block".into())}else{Ok(out)}}
            if b[*p]==b'}'{if !close{return Err("Unexpected CSG closing brace".into())}*p+=1;return Ok(out)}
            let start=*p;let mut par=0i32;let mut string=false;let mut escape=false;
            while *p<b.len(){
                let ch=b[*p];
                if string {if escape{escape=false}else if ch==b'\\'{escape=true}else if ch==b'"'{string=false}}
                else {match ch {b'"'=>string=true,b'('|b'['=>par+=1,b')'|b']'=>par-=1,b'{'|b';' if par==0=>break,_=>{}}}
                *p+=1;
            }
            if *p>=b.len(){return Err("Incomplete evaluated CSG statement".into())}
            let head=s[start..*p].trim().to_owned();let block=b[*p]==b'{';*p+=1;
            let children=if block{list(s,p,true)?}else{Vec::new()};
            out.push(Node{head,code:s[start..*p].to_owned(),children});
        }
    }
    list(source,&mut 0,false)
}
fn block(head:&str,codes:impl IntoIterator<Item=String>)->String{format!("{} {{\n{}\n}}",head,codes.into_iter().collect::<Vec<_>>().join("\n"))}
fn merge(layers:impl IntoIterator<Item=Vec<Layer>>)->Vec<Layer>{
    let mut grouped:Vec<(Color,Vec<String>)>=Vec::new();
    for layer in layers {for part in layer {if let Some((_,v))=grouped.iter_mut().find(|(c,_)|*c==part.color){v.push(part.code)}else{grouped.push((part.color,vec![part.code]))}}}
    grouped.into_iter().map(|(color,mut v)|Layer{color,code:if v.len()==1{v.remove(0)}else{block("union()",v)}}).collect()
}
fn walk(n:&Node,inherited:Option<Color>,warnings:&mut Vec<String>)->Result<Vec<Layer>>{
    if n.head.trim_start().starts_with(['%','*']){return Ok(vec![])}
    let name=n.name();
    if name=="color" {
        let color=if inherited.is_some(){inherited}else{
            let a=n.head.find('[').ok_or("Missing CSG color")?;let z=n.head[a..].find(']').ok_or("Missing color end")?+a;
            let values:Vec<f64>=serde_json::from_str(&n.head[a..=z]).map_err(err)?;
            if values.len()<3{return Err("Invalid CSG color".into())}
            if values[..3].iter().any(|x|*x<0.){None}else{Some([values[0],values[1],values[2],*values.get(3).unwrap_or(&1.)])}
        };
        return Ok(merge(n.children.iter().map(|c|walk(c,color,warnings)).collect::<Result<Vec<_>>>()?))
    }
    if name=="group"||name=="union"||name.is_empty(){return Ok(merge(n.children.iter().map(|c|walk(c,inherited,warnings)).collect::<Result<Vec<_>>>()?))}
    if n.children.is_empty(){return Ok(if ["difference","intersection","hull","minkowski"].contains(&name){vec![]}else{vec![Layer{color:inherited.unwrap_or(DEFAULT),code:n.code.clone()}]})}
    if name=="difference"||name=="intersection"{
        return Ok(walk(&n.children[0],inherited,warnings)?.into_iter().map(|l|Layer{color:l.color,code:block(&format!("{}()",name),std::iter::once(l.code).chain(n.children[1..].iter().map(|c|c.code.clone())))}).collect())
    }
    let layers=merge(n.children.iter().map(|c|walk(c,inherited,warnings)).collect::<Result<Vec<_>>>()?);
    if layers.len()<=1{return Ok(layers.into_iter().map(|l|Layer{color:l.color,code:n.code.clone()}).collect())}
    if ["hull","minkowski","offset","fill","resize","roof"].contains(&name){warnings.push(format!("WARNING: {} combines materials; generated geometry uses first material. Wrap color() to override.",name));return Ok(vec![Layer{color:layers[0].color,code:n.code.clone()}])}
    Ok(layers.into_iter().map(|l|Layer{color:l.color,code:block(&n.head,[l.code])}).collect())
}
fn plan(s:&str)->Result<(Vec<Layer>,Vec<String>)>{let mut warnings=Vec::new();let layers=merge(parse(s)?.iter().map(|n|walk(n,None,&mut warnings)).collect::<Result<Vec<_>>>()?);Ok((layers,warnings))}
fn run(exe:&str,dir:&Path,source:&str,output:&Path,backend:bool,binary:bool)->Result<String>{
    let input=Builder::new().prefix(".scadforge-").suffix(".scad").tempfile_in(dir).map_err(err)?.into_temp_path();
    fs::write(&input,source).map_err(err)?;
    let mut c=command(exe);c.current_dir(dir).arg("-o").arg(output);
    if backend{c.arg("--backend=Manifold");}if binary{c.args(["--export-format","binstl"]);}
    let log_file=Builder::new().prefix(".scadforge-log-").tempfile().map_err(err)?;
    let log_handle=log_file.reopen().map_err(err)?;c.stdout(log_handle.try_clone().map_err(err)?).stderr(log_handle);
    let mut child=c.arg(&input).spawn().map_err(err)?;let start=Instant::now();
    let limit=std::env::var("SCADFORGE_TIMEOUT_SECONDS").ok().and_then(|v|v.parse::<u64>().ok()).unwrap_or(300).clamp(5,3600);
    let status=loop{if let Some(status)=child.try_wait().map_err(err)?{break status}if start.elapsed().as_secs()>limit{let _=child.kill();let _=child.wait();return Err(format!("OpenSCAD exceeded {} seconds; reduce preview detail or use Manifold.",limit))}thread::sleep(Duration::from_millis(40));};
    let log=fs::read_to_string(log_file.path()).map_err(err)?;
    if !status.success() && !(log.contains("Current top level object is empty")&&!log.contains("ERROR:")){return Err(log)}
    Ok(log)
}
fn digest(code:&str,signature:&str,dir:&Path)->Result<String>{
    let mut h=Sha256::new();h.update(b"forge-engine-v1\0");h.update(signature);h.update(code);
    // Imported meshes/surfaces are not expanded into CSG. Their bytes must be
    // included in cache identity, even when their filenames have not changed.
    let re=regex::Regex::new(r#"file\s*=\s*("(?:\\.|[^"\\])*")"#).map_err(err)?;
    for cap in re.captures_iter(code){let file:String=serde_json::from_str(&cap[1]).map_err(err)?;let path=dir.join(file);h.update(fs::read(&path).map_err(|e|format!("{}: {}",path.display(),e))?);}
    Ok(format!("{:x}",h.finalize()))
}
fn compile_task(task:&Task,request:&Request,manifold:bool)->Result<MeshResult>{
    let final_path=request.cache.join(format!("{}.stl",task.key));let empty=request.cache.join(format!("{}.empty",task.key));let logfile=request.cache.join(format!("{}.log",task.key));
    if final_path.is_file(){return Ok(MeshResult{path:Some(final_path),cached:true,log:fs::read_to_string(logfile).unwrap_or_default()})}
    if empty.is_file(){return Ok(MeshResult{path:None,cached:true,log:fs::read_to_string(logfile).unwrap_or_default()})}
    let output=Builder::new().prefix(".pending-").suffix(".stl").tempfile_in(&request.cache).map_err(err)?.into_temp_path();
    fs::remove_file(&output).map_err(err)?;
    let log=run(&request.executable,&request.directory,&task.code,&output,manifold,true)?;
    let path=if output.is_file() && fs::metadata(&output).map_err(err)?.len()>84 {
        if !final_path.exists(){fs::rename(&output,&final_path).map_err(err)?;}Some(final_path)
    }else{fs::write(empty,b"empty").map_err(err)?;None};
    let _=fs::write(logfile,&log);
    Ok(MeshResult{path,cached:false,log})
}
fn compile(request:Request)->Result<()> {
    let started=Instant::now();fs::create_dir_all(&request.cache).map_err(err)?;
    let help=command(&request.executable).arg("--help").output().map_err(err)?;
    let text=format!("{}{}",String::from_utf8_lossy(&help.stdout),String::from_utf8_lossy(&help.stderr)).to_lowercase();
    let manifold=text.contains("--backend")&&text.contains("manifold");
    let version=command(&request.executable).arg("--version").output().map_err(err)?;
    let exe=fs::metadata(&request.executable).ok().and_then(|m|m.modified().ok());
    let signature=format!("{} {:?} {} {} {}",request.executable,exe,String::from_utf8_lossy(&version.stdout),String::from_utf8_lossy(&version.stderr),manifold);
    event(json!({"event":"progress","percent":0,"phase":format!("Rust · {} · expanding modules",if manifold{"Manifold"}else{"CGAL (Manifold unavailable)"})}));
    let temp=tempfile::tempdir().map_err(err)?;let mut frames=Vec::new();let mut tasks:Vec<Task>=Vec::new();let mut indices=HashMap::new();let mut logs=String::new();let roots=request.frames.iter().map(|f|f.len()).sum::<usize>().max(1);let mut expanded=0;
    let tessellation=regex::Regex::new(r"\$fn\s*=\s*([0-9]+(?:\.[0-9]+)?)").map_err(err)?;
    let mut add=|code:String|->Result<usize>{let key=digest(&code,&signature,&request.directory)?;if let Some(index)=indices.get(&key){return Ok(*index)}let index=tasks.len();indices.insert(key.clone(),index);tasks.push(Task{key,code});Ok(index)};
    for inputs in &request.frames {
        let mut chunks=Vec::new();
        for root in inputs {
            let out=temp.path().join("expanded.csg");
            logs.push_str(&run(&request.executable,&request.directory,&root.source,&out,false,false)?);
            let mut code=fs::read_to_string(&out).map_err(err)?;
            if request.draft{code=tessellation.replace_all(&code,|cap:&regex::Captures|format!("$fn = {}",cap[1].parse::<f64>().unwrap_or(24.).min(24.))).to_string();}
            let (layers,warnings)=plan(&code)?;for warning in warnings{logs.push_str(&warning);logs.push('\n');}
            let solid=add(code)?;let mut parts=Vec::new();
            if layers.len()==1 {parts.push((layers[0].color,solid));}
            else {for layer in layers{parts.push((layer.color,add(layer.code)?));}}
            chunks.push(Chunk{start:root.start,end:root.end,solid,parts});expanded+=1;
            event(json!({"event":"progress","percent":10*expanded/roots,"phase":format!("Expanded {}/{} objects/frames",expanded,roots)}));
        }
        frames.push(chunks);
    }
    let count=tasks.len();let queue=Arc::new(Mutex::new(VecDeque::from_iter(tasks.into_iter().enumerate())));let (tx,rx)=mpsc::channel();let mut results:Vec<Option<MeshResult>>=vec![None;count];let mut first_error=None;
    thread::scope(|scope|{
        for _ in 0..request.workers.clamp(1,8).min(count.max(1)) {
            let q=queue.clone();let tx=tx.clone();let r=&request;
            scope.spawn(move||{loop{let task=q.lock().unwrap().pop_front();let Some((index,task))=task else{break};let result=compile_task(&task,r,manifold);if tx.send((index,result)).is_err(){break}}});
        }
        drop(tx);let mut done=0;let mut cached=0;
        for (index,result) in rx {done+=1;match result {Ok(mesh)=>{if mesh.cached{cached+=1}results[index]=Some(mesh)},Err(e)=>{first_error=Some(e);queue.lock().unwrap().clear();}}
            event(json!({"event":"progress","percent":10+88*done/count.max(1),"completed":done,"total":count,"phase":format!("Rust · {}/{} meshes · {} cached",done,count,cached)}));
        }
    });
    if let Some(e)=first_error{return Err(e)}
    let results=results.into_iter().map(|m|m.ok_or("Missing task result".to_string())).collect::<Result<Vec<_>>>()?;
    for result in &results{logs.push_str(&result.log)}
    event(json!({"event":"done","frames":frames,"meshes":results,"seconds":started.elapsed().as_secs_f64(),"backend":if manifold{"Manifold"}else{"CGAL"},"logs":logs}));Ok(())
}
fn main(){
    if std::env::args().any(|a|a=="--version"){println!("scadforge-engine 0.7.0");return}
    let mut input=String::new();let result=io::stdin().read_to_string(&mut input).map_err(err).and_then(|_|serde_json::from_str::<Request>(&input).map_err(err)).and_then(compile);
    if let Err(message)=result{event(json!({"event":"error","message":message}));std::process::exit(1)}
}
#[cfg(test)] mod tests{
    use super::*;
    #[test]fn nested_colors_and_default(){let (p,_)=plan("group(){color([0,0,1,1]){cube(size=[2,2,2]);}multmatrix([[1,0,0,5],[0,1,0,0],[0,0,1,0],[0,0,0,1]]){cube(size=[2,2,2]);}}").unwrap();assert_eq!(p.len(),2);assert_eq!(p[0].color,[0.,0.,1.,1.]);assert_eq!(p[1].color,DEFAULT);}
    #[test]fn parent_overrides_child(){let(p,_)=plan("color([1,0,0,1]){group(){color([0,0,1,1]){cube(2);}cube(3);}}").unwrap();assert_eq!(p.len(),1);assert_eq!(p[0].color,[1.,0.,0.,1.]);}
    #[test]fn cutters_never_add_material(){let(p,_)=plan("difference(){color([1,0,0,1]){cube(3);}color([0,1,0,1]){sphere(2);}}").unwrap();assert_eq!(p.len(),1);assert!(p[0].code.starts_with("difference()"));}
    #[test]fn strings_and_modifiers(){assert_eq!(parse("%color([1,0,0,1]){import(file=\"a{b}.stl\");}").unwrap().len(),1);assert!(plan("%color([1,0,0,1]){cube(1);}").unwrap().0.is_empty());}
    #[test]fn import_bytes_invalidate_hash(){let t=tempfile::tempdir().unwrap();fs::write(t.path().join("x.stl"),b"old").unwrap();let a=digest("import(file=\"x.stl\");","test",t.path()).unwrap();fs::write(t.path().join("x.stl"),b"new").unwrap();assert_ne!(a,digest("import(file=\"x.stl\");","test",t.path()).unwrap());}
}
