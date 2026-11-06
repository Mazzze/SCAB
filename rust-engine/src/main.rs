//! SCAD Forge native compilation scheduler. JSON-lines protocol on stdout.
//!
//! Pipeline per animation frame:
//!   1. ONE OpenSCAD run expands the whole design to evaluated CSG. Each
//!      top-level object was wrapped by the UI in `render(convexity = MARK+i)`
//!      so its subtree can be found again in the CSG output.
//!   2. Each object's CSG is split into *pieces*: unions, groups, colors and
//!      affine transforms are distributed; difference/intersection keep their
//!      cutters; hull/minkowski/extrusions and primitives are atomic.
//!   3. Pieces are instanced: geometry is keyed by its code without the
//!      accumulated transform, so identical parts (vias, screws, repeated
//!      components) are meshed once and placed by matrix in the UI.
//!   4. Unique geometry is meshed in parallel worker processes and cached by
//!      content hash (code + imported file bytes + OpenSCAD identity).
//! All geometry is still produced by standard OpenSCAD.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::Command,
    sync::{mpsc, Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
use tempfile::Builder;

type Result<T> = std::result::Result<T, String>;
type Color = [f64; 4];
type Matrix = [f64; 16];
const DEFAULT: Color = [0.38, 0.75, 0.73, 1.];
const IDENTITY: Matrix = [1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.];
pub const MARK: u64 = 9876500;

#[derive(Deserialize)]
struct RootSpan {
    start: usize,
    end: usize,
}
#[derive(Deserialize)]
struct FrameRequest {
    source: String,
    roots: Vec<RootSpan>,
}
#[derive(Deserialize)]
struct Request {
    executable: String,
    directory: PathBuf,
    cache: PathBuf,
    frames: Vec<FrameRequest>,
    #[serde(default = "two")]
    workers: usize,
    #[serde(default)]
    draft: bool,
    #[serde(default = "max_tasks")]
    max_tasks: usize,
}
fn two() -> usize {
    2
}
fn max_tasks() -> usize {
    384
}

#[derive(Clone, Debug)]
struct Node {
    head: String,
    code: String,
    children: Vec<Node>,
}
impl Node {
    fn name(&self) -> &str {
        self.head.split('(').next().unwrap_or("").trim().trim_start_matches(['!', '#', '%', '*'])
    }
}
/// A colored piece of geometry: `code` placed by `matrix`.
#[derive(Clone, Debug)]
struct Piece {
    color: Color,
    matrix: Matrix,
    code: String,
}
#[derive(Serialize)]
struct Part {
    color: Color,
    mesh: usize,
    matrix: Option<Matrix>,
}
#[derive(Serialize)]
struct Chunk {
    start: usize,
    end: usize,
    parts: Vec<Part>,
}
#[derive(Clone)]
struct Task {
    key: String,
    code: String,
}
#[derive(Clone, Serialize)]
struct MeshResult {
    path: Option<PathBuf>,
    cached: bool,
    log: String,
}

fn event(v: Value) {
    println!("{}", v);
    let _ = io::stdout().flush();
}
fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}
fn command(exe: &str) -> Command {
    let mut c = Command::new(exe);
    c.env("QT_QPA_PLATFORM", "offscreen");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x08000000);
    }
    c
}

// ------------------------------------------------------------------ CSG parse
fn parse(source: &str) -> Result<Vec<Node>> {
    fn list(s: &str, p: &mut usize, close: bool) -> Result<Vec<Node>> {
        let b = s.as_bytes();
        let mut out = Vec::new();
        loop {
            while *p < b.len() && b[*p].is_ascii_whitespace() {
                *p += 1;
            }
            if *p == b.len() {
                return if close { Err("Unclosed CSG block".into()) } else { Ok(out) };
            }
            if b[*p] == b'}' {
                if !close {
                    return Err("Unexpected CSG closing brace".into());
                }
                *p += 1;
                return Ok(out);
            }
            let start = *p;
            let mut par = 0i32;
            let mut string = false;
            let mut escape = false;
            while *p < b.len() {
                let ch = b[*p];
                if string {
                    if escape {
                        escape = false
                    } else if ch == b'\\' {
                        escape = true
                    } else if ch == b'"' {
                        string = false
                    }
                } else {
                    match ch {
                        b'"' => string = true,
                        b'(' | b'[' => par += 1,
                        b')' | b']' => par -= 1,
                        b'{' | b';' if par == 0 => break,
                        _ => {}
                    }
                }
                *p += 1;
            }
            if *p >= b.len() {
                return Err("Incomplete evaluated CSG statement".into());
            }
            let head = s[start..*p].trim().to_owned();
            let block = b[*p] == b'{';
            *p += 1;
            let children = if block { list(s, p, true)? } else { Vec::new() };
            out.push(Node { head, code: s[start..*p].to_owned(), children });
        }
    }
    list(source, &mut 0, false)
}

fn block(head: &str, codes: impl IntoIterator<Item = String>) -> String {
    format!("{} {{\n{}\n}}", head, codes.into_iter().collect::<Vec<_>>().join("\n"))
}

fn multiply(a: &Matrix, b: &Matrix) -> Matrix {
    let mut m = [0.; 16];
    for r in 0..4 {
        for c in 0..4 {
            m[r * 4 + c] = (0..4).map(|k| a[r * 4 + k] * b[k * 4 + c]).sum();
        }
    }
    m
}
fn parse_matrix(head: &str) -> Option<Matrix> {
    let a = head.find('[')?;
    let z = head.rfind(']')?;
    let rows: Vec<Vec<f64>> = serde_json::from_str(&head[a..=z]).ok()?;
    if rows.len() < 3 {
        return None;
    }
    let mut m = IDENTITY;
    for (r, row) in rows.iter().take(4).enumerate() {
        for (c, v) in row.iter().take(4).enumerate() {
            m[r * 4 + c] = *v;
        }
    }
    Some(m)
}
fn matrix_code(m: &Matrix) -> String {
    let rows: Vec<String> = (0..4)
        .map(|r| format!("[{}]", (0..4).map(|c| format!("{}", m[r * 4 + c])).collect::<Vec<_>>().join(", ")))
        .collect();
    format!("multmatrix([{}])", rows.join(", "))
}
fn place(piece: &Piece) -> String {
    if piece.matrix == IDENTITY {
        piece.code.clone()
    } else {
        block(&matrix_code(&piece.matrix), [piece.code.clone()])
    }
}
fn parse_color(head: &str) -> Result<Option<Color>> {
    let a = head.find('[').ok_or("Missing CSG color")?;
    let z = head[a..].find(']').ok_or("Missing color end")? + a;
    let values: Vec<f64> = serde_json::from_str(&head[a..=z]).map_err(err)?;
    if values.len() < 3 {
        return Err("Invalid CSG color".into());
    }
    Ok(if values[..3].iter().any(|x| *x < 0.) {
        None
    } else {
        Some([values[0], values[1], values[2], *values.get(3).unwrap_or(&1.)])
    })
}

/// Merge same-colored pieces (all placed) into one piece per color.
fn merge_by_color(pieces: Vec<Piece>) -> Vec<Piece> {
    let mut grouped: Vec<(Color, Vec<String>)> = Vec::new();
    for piece in pieces {
        let code = place(&piece);
        if let Some((_, v)) = grouped.iter_mut().find(|(c, _)| *c == piece.color) {
            v.push(code)
        } else {
            grouped.push((piece.color, vec![code]))
        }
    }
    grouped
        .into_iter()
        .map(|(color, mut v)| Piece {
            color,
            matrix: IDENTITY,
            code: if v.len() == 1 { v.remove(0) } else { block("union()", v) },
        })
        .collect()
}

/// Split evaluated CSG into colored, transformed pieces.
fn walk(n: &Node, inherited: Option<Color>, matrix: &Matrix, warnings: &mut Vec<String>) -> Result<Vec<Piece>> {
    if n.head.trim_start().starts_with(['%', '*']) {
        return Ok(vec![]);
    }
    let name = n.name();
    let children = |inherited: Option<Color>, matrix: &Matrix, warnings: &mut Vec<String>| -> Result<Vec<Piece>> {
        let mut out = Vec::new();
        for c in &n.children {
            out.extend(walk(c, inherited, matrix, warnings)?);
        }
        Ok(out)
    };
    if name == "color" {
        let color = if inherited.is_some() { inherited } else { parse_color(&n.head)? };
        return children(color, matrix, warnings);
    }
    if name == "group" || name == "union" || name.is_empty() || (name == "render" && !n.children.is_empty()) {
        return children(inherited, matrix, warnings);
    }
    if name == "multmatrix" {
        if let Some(m) = parse_matrix(&n.head) {
            return children(inherited, &multiply(matrix, &m), warnings);
        }
    }
    if n.children.is_empty() {
        if ["difference", "intersection", "hull", "minkowski", "render", "group", "union", "color", "multmatrix"].contains(&name) {
            return Ok(vec![]);
        }
        return Ok(vec![Piece { color: inherited.unwrap_or(DEFAULT), matrix: *matrix, code: n.code.clone() }]);
    }
    if name == "difference" || name == "intersection" {
        // Cutters constrain every material of the first operand and never add color.
        let base = merge_by_color(walk(&n.children[0], inherited, &IDENTITY, warnings)?);
        let rest: Vec<String> = n.children[1..].iter().map(|c| c.code.clone()).collect();
        return Ok(base
            .into_iter()
            .map(|l| Piece {
                color: l.color,
                matrix: *matrix,
                code: block(&format!("{}()", name), std::iter::once(l.code).chain(rest.iter().cloned())),
            })
            .collect());
    }
    let layers = merge_by_color(children(inherited, &IDENTITY, warnings)?);
    if layers.len() <= 1 {
        let color = layers.first().map(|l| l.color).unwrap_or(inherited.unwrap_or(DEFAULT));
        return Ok(vec![Piece { color, matrix: *matrix, code: n.code.clone() }]);
    }
    if ["hull", "minkowski", "offset", "fill", "resize", "roof", "projection"].contains(&name) {
        warnings.push(format!(
            "WARNING: {} combines materials; generated geometry uses the first material. Wrap color() to override.",
            name
        ));
        return Ok(vec![Piece { color: layers[0].color, matrix: *matrix, code: n.code.clone() }]);
    }
    // Extrusions and other operators distribute over the material layers.
    Ok(layers
        .into_iter()
        .map(|l| Piece { color: l.color, matrix: *matrix, code: block(&n.head, [l.code]) })
        .collect())
}

fn plan(n: &Node) -> Result<(Vec<Piece>, Vec<String>)> {
    let mut warnings = Vec::new();
    let pieces = walk(n, None, &IDENTITY, &mut warnings)?;
    Ok((pieces, warnings))
}

/// Find `render(convexity = MARK+i)` wrappers; returns index -> children code.
fn find_roots(nodes: &[Node], out: &mut HashMap<usize, Node>) {
    for n in nodes {
        if n.name() == "render" {
            if let Some(index) = marker(&n.head) {
                out.insert(index, Node { head: "group()".into(), code: block("group()", n.children.iter().map(|c| c.code.clone())), children: n.children.clone() });
                continue;
            }
        }
        find_roots(&n.children, out);
    }
}
fn marker(head: &str) -> Option<usize> {
    let a = head.find("convexity")?;
    let rest = &head[a + 9..];
    let digits: String = rest.chars().skip_while(|c| !c.is_ascii_digit()).take_while(|c| c.is_ascii_digit()).collect();
    let value: u64 = digits.parse().ok()?;
    if value >= MARK && value < MARK + 100000 {
        Some((value - MARK) as usize)
    } else {
        None
    }
}

// ----------------------------------------------------------------- execution
fn run(exe: &str, dir: &Path, source: &str, output: &Path, backend: bool, binary: bool) -> Result<String> {
    let input = Builder::new().prefix(".scadforge-").suffix(".scad").tempfile_in(dir).map_err(err)?.into_temp_path();
    fs::write(&input, source).map_err(err)?;
    let mut c = command(exe);
    c.current_dir(dir).arg("-o").arg(output);
    if backend {
        c.arg("--backend=Manifold");
    }
    if binary {
        c.args(["--export-format", "binstl"]);
    }
    let log_file = Builder::new().prefix(".scadforge-log-").tempfile().map_err(err)?;
    let log_handle = log_file.reopen().map_err(err)?;
    c.stdout(log_handle.try_clone().map_err(err)?).stderr(log_handle);
    let mut child = c.arg(&input).spawn().map_err(err)?;
    let start = Instant::now();
    let limit = std::env::var("SCADFORGE_TIMEOUT_SECONDS").ok().and_then(|v| v.parse::<u64>().ok()).unwrap_or(300).clamp(5, 3600);
    let status = loop {
        if let Some(status) = child.try_wait().map_err(err)? {
            break status;
        }
        if start.elapsed().as_secs() > limit {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("OpenSCAD exceeded {} seconds; reduce preview detail or use Manifold.", limit));
        }
        thread::sleep(Duration::from_millis(15));
    };
    let log = fs::read_to_string(log_file.path()).map_err(err)?;
    if !status.success() && !(log.contains("Current top level object is empty") && !log.contains("ERROR:")) {
        return Err(log);
    }
    Ok(log)
}

fn digest(code: &str, signature: &str, dir: &Path) -> Result<String> {
    let mut h = Sha256::new();
    h.update(b"forge-engine-v2\0");
    h.update(signature);
    h.update(code);
    // Imported meshes/surfaces are not expanded into CSG. Their bytes must be
    // included in cache identity, even when their filenames have not changed.
    let re = regex::Regex::new(r#"file\s*=\s*("(?:\\.|[^"\\])*")"#).map_err(err)?;
    for cap in re.captures_iter(code) {
        let file: String = serde_json::from_str(&cap[1]).map_err(err)?;
        let path = dir.join(file);
        h.update(fs::read(&path).map_err(|e| format!("{}: {}", path.display(), e))?);
    }
    Ok(format!("{:x}", h.finalize()))
}

fn compile_task(task: &Task, request: &Request, manifold: bool) -> Result<MeshResult> {
    let final_path = request.cache.join(format!("{}.stl", task.key));
    let empty = request.cache.join(format!("{}.empty", task.key));
    let logfile = request.cache.join(format!("{}.log", task.key));
    if final_path.is_file() {
        return Ok(MeshResult { path: Some(final_path), cached: true, log: fs::read_to_string(logfile).unwrap_or_default() });
    }
    if empty.is_file() {
        return Ok(MeshResult { path: None, cached: true, log: fs::read_to_string(logfile).unwrap_or_default() });
    }
    let output = Builder::new().prefix(".pending-").suffix(".stl").tempfile_in(&request.cache).map_err(err)?.into_temp_path();
    fs::remove_file(&output).map_err(err)?;
    let log = run(&request.executable, &request.directory, &task.code, &output, manifold, true)?;
    let path = if output.is_file() && fs::metadata(&output).map_err(err)?.len() > 84 {
        if !final_path.exists() {
            fs::rename(&output, &final_path).map_err(err)?;
        }
        Some(final_path)
    } else {
        fs::write(empty, b"empty").map_err(err)?;
        None
    };
    let _ = fs::write(logfile, &log);
    Ok(MeshResult { path, cached: false, log })
}

/// OpenSCAD identity (path, mtime, version, Manifold support), cached on disk:
/// probing spawns two processes, which is noticeable on every keystroke compile.
fn signature(request: &Request) -> Result<(String, bool)> {
    let meta = fs::metadata(&request.executable).ok();
    let stamp = format!(
        "{} {:?} {:?}",
        request.executable,
        meta.as_ref().and_then(|m| m.modified().ok()),
        meta.as_ref().map(|m| m.len())
    );
    let mut h = Sha256::new();
    h.update(&stamp);
    let file = request.cache.join(format!("openscad-{:x}.probe", h.finalize()));
    if let Ok(text) = fs::read_to_string(&file) {
        if let Some((flag, sig)) = text.split_once('\n') {
            return Ok((sig.to_string(), flag == "manifold"));
        }
    }
    let help = command(&request.executable).arg("--help").output().map_err(err)?;
    let text = format!("{}{}", String::from_utf8_lossy(&help.stdout), String::from_utf8_lossy(&help.stderr)).to_lowercase();
    let manifold = text.contains("--backend") && text.contains("manifold");
    let version = command(&request.executable).arg("--version").output().map_err(err)?;
    let sig = format!(
        "{} {} {} {}",
        stamp,
        String::from_utf8_lossy(&version.stdout).trim(),
        String::from_utf8_lossy(&version.stderr).trim(),
        manifold
    );
    let _ = fs::write(&file, format!("{}\n{}", if manifold { "manifold" } else { "cgal" }, sig));
    Ok((sig, manifold))
}

/// Keep at most `limit` tasks: frequent (instanced) geometry stays separate,
/// the remaining one-off pieces are bundled in order into unions per color.
fn bundle(pieces: Vec<Piece>, limit: usize) -> Vec<Piece> {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for p in &pieces {
        *counts.entry(p.code.as_str()).or_default() += 1;
    }
    if counts.len() <= limit {
        return pieces;
    }
    let mut ranked: Vec<(&str, usize)> = counts.iter().map(|(k, v)| (*k, *v)).collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.len().cmp(&b.0.len())));
    let keep: std::collections::HashSet<String> =
        ranked.iter().take(limit / 2).filter(|(_, n)| *n > 1).map(|(k, _)| k.to_string()).collect();
    let mut out = Vec::new();
    let mut rest: Vec<Piece> = Vec::new();
    for p in pieces {
        if keep.contains(&p.code) {
            out.push(p)
        } else {
            rest.push(p)
        }
    }
    let slots = (limit - keep.len().min(limit)).max(1);
    let per = (rest.len() + slots - 1) / slots;
    let mut i = 0;
    while i < rest.len() {
        let chunk = &rest[i..(i + per).min(rest.len())];
        out.extend(merge_by_color(chunk.to_vec()));
        i += per;
    }
    out
}

fn compile(request: Request) -> Result<()> {
    let started = Instant::now();
    fs::create_dir_all(&request.cache).map_err(err)?;
    let (signature, manifold) = signature(&request)?;
    event(json!({"event":"progress","percent":0,"phase":format!("Rust · {} · expanding design",if manifold{"Manifold"}else{"CGAL (Manifold unavailable)"})}));
    let temp = tempfile::tempdir().map_err(err)?;
    let mut frames = Vec::new();
    let mut tasks: Vec<Task> = Vec::new();
    let mut indices: HashMap<String, usize> = HashMap::new();
    let mut logs = String::new();
    let mut expand_logs = String::new();
    let tessellation = regex::Regex::new(r"\$fn\s*=\s*([0-9]+(?:\.[0-9]+)?)").map_err(err)?;
    let fragments = regex::Regex::new(r"\$fs\s*=\s*([0-9]+(?:\.[0-9]+)?)").map_err(err)?;
    let total_frames = request.frames.len().max(1);
    let mut piece_total = 0usize;
    let mut instanced = 0usize;
    let mut add = |code: String, tasks: &mut Vec<Task>| -> Result<usize> {
        let key = digest(&code, &signature, &request.directory)?;
        if let Some(index) = indices.get(&key) {
            return Ok(*index);
        }
        let index = tasks.len();
        indices.insert(key.clone(), index);
        tasks.push(Task { key, code });
        Ok(index)
    };
    for (frame_index, frame) in request.frames.iter().enumerate() {
        let out = temp.path().join(format!("expanded-{}.csg", frame_index));
        let log = run(&request.executable, &request.directory, &frame.source, &out, false, false)?;
        expand_logs.push_str(&log);
        let mut code = fs::read_to_string(&out).unwrap_or_default();
        if request.draft {
            code = tessellation
                .replace_all(&code, |cap: &regex::Captures| format!("$fn = {}", cap[1].parse::<f64>().unwrap_or(24.).min(24.)))
                .to_string();
            code = fragments
                .replace_all(&code, |cap: &regex::Captures| format!("$fs = {}", cap[1].parse::<f64>().unwrap_or(2.).max(1.)))
                .to_string();
        }
        let tree = parse(&code)?;
        let mut roots = HashMap::new();
        find_roots(&tree, &mut roots);
        let mut chunks = Vec::new();
        for (i, span) in frame.roots.iter().enumerate() {
            let mut parts = Vec::new();
            if let Some(node) = roots.get(&i) {
                let (pieces, warnings) = plan(node)?;
                for warning in warnings {
                    logs.push_str(&warning);
                    logs.push('\n');
                }
                piece_total += pieces.len();
                let pieces = bundle(pieces, request.max_tasks);
                for piece in pieces {
                    let before = tasks.len();
                    let mesh = add(piece.code.clone(), &mut tasks)?;
                    if tasks.len() == before {
                        instanced += 1;
                    }
                    parts.push(Part { color: piece.color, mesh, matrix: if piece.matrix == IDENTITY { None } else { Some(piece.matrix) } });
                }
            }
            chunks.push(Chunk { start: span.start, end: span.end, parts });
        }
        frames.push(chunks);
        event(json!({"event":"progress","percent":10*(frame_index+1)/total_frames,"phase":format!("Expanded frame {}/{} · {} pieces · {} unique meshes",frame_index+1,total_frames,piece_total,tasks.len())}));
    }
    let count = tasks.len();
    let queue = Arc::new(Mutex::new(VecDeque::from_iter(tasks.into_iter().enumerate())));
    let (tx, rx) = mpsc::channel();
    let mut results: Vec<Option<MeshResult>> = vec![None; count];
    let mut first_error = None;
    thread::scope(|scope| {
        for _ in 0..request.workers.clamp(1, 32).min(count.max(1)) {
            let q = queue.clone();
            let tx = tx.clone();
            let r = &request;
            scope.spawn(move || loop {
                let task = q.lock().unwrap().pop_front();
                let Some((index, task)) = task else { break };
                let result = compile_task(&task, r, manifold);
                if tx.send((index, result)).is_err() {
                    break;
                }
            });
        }
        drop(tx);
        let mut done = 0;
        let mut cached = 0;
        for (index, result) in rx {
            done += 1;
            match result {
                Ok(mesh) => {
                    if mesh.cached {
                        cached += 1
                    }
                    results[index] = Some(mesh)
                }
                Err(e) => {
                    first_error = Some(e);
                    queue.lock().unwrap().clear();
                }
            }
            event(json!({"event":"progress","percent":10+88*done/count.max(1),"completed":done,"total":count,"phase":format!("Rust · {}/{} meshes · {} cached",done,count,cached)}));
        }
    });
    if let Some(e) = first_error {
        return Err(e);
    }
    let results = results.into_iter().map(|m| m.ok_or("Missing task result".to_string())).collect::<Result<Vec<_>>>()?;
    for result in &results {
        logs.push_str(&result.log)
    }
    event(json!({"event":"done","frames":frames,"meshes":results,"seconds":started.elapsed().as_secs_f64(),
        "backend":if manifold{"Manifold"}else{"CGAL"},"logs":logs,"expand_logs":expand_logs,
        "pieces":piece_total,"instanced":instanced}));
    Ok(())
}

fn main() {
    if std::env::args().any(|a| a == "--version") {
        println!("scadforge-engine 0.8.0");
        return;
    }
    let mut input = String::new();
    let result = io::stdin()
        .read_to_string(&mut input)
        .map_err(err)
        .and_then(|_| serde_json::from_str::<Request>(&input).map_err(err))
        .and_then(compile);
    if let Err(message) = result {
        event(json!({"event":"error","message":message}));
        std::process::exit(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn pieces(s: &str) -> Vec<Piece> {
        let tree = parse(s).unwrap();
        let root = Node { head: "group()".into(), code: s.into(), children: tree };
        plan(&root).unwrap().0
    }
    #[test]
    fn nested_colors_and_default() {
        let p = pieces("group(){color([0,0,1,1]){cube(size=[2,2,2]);}multmatrix([[1,0,0,5],[0,1,0,0],[0,0,1,0],[0,0,0,1]]){cube(size=[2,2,2]);}}");
        assert_eq!(p.len(), 2);
        assert_eq!(p[0].color, [0., 0., 1., 1.]);
        assert_eq!(p[1].color, DEFAULT);
        assert_eq!(p[1].matrix[3], 5.);
        assert_eq!(p[0].code, p[1].code, "identical cubes are instanced");
    }
    #[test]
    fn parent_overrides_child() {
        let p = pieces("color([1,0,0,1]){group(){color([0,0,1,1]){cube(2);}cube(3);}}");
        assert!(p.iter().all(|x| x.color == [1., 0., 0., 1.]));
    }
    #[test]
    fn cutters_never_add_material() {
        let p = pieces("difference(){color([1,0,0,1]){cube(3);}color([0,1,0,1]){sphere(2);}}");
        assert_eq!(p.len(), 1);
        assert!(p[0].code.starts_with("difference()"));
        assert_eq!(p[0].color, [1., 0., 0., 1.]);
    }
    #[test]
    fn transforms_compose_and_distribute() {
        let p = pieces("multmatrix([[1,0,0,1],[0,1,0,0],[0,0,1,0],[0,0,0,1]]){group(){multmatrix([[1,0,0,2],[0,1,0,0],[0,0,1,0],[0,0,0,1]]){cylinder(h=1,r1=1,r2=1);}cube(1);}}");
        assert_eq!(p.len(), 2);
        assert_eq!(p[0].matrix[3], 3.);
        assert_eq!(p[1].matrix[3], 1.);
        assert!(p[0].code.starts_with("cylinder"));
    }
    #[test]
    fn markers_find_roots() {
        let tree = parse("render(convexity = 9876500) { cube(1); } render(convexity = 9876501); render(convexity = 9876502) { sphere(1); }").unwrap();
        let mut roots = HashMap::new();
        find_roots(&tree, &mut roots);
        assert_eq!(roots.len(), 3);
        assert!(roots[&1].children.is_empty());
        assert!(roots[&2].code.contains("sphere"));
    }
    #[test]
    fn bundling_limits_tasks() {
        let mut ps = Vec::new();
        for i in 0..100 {
            ps.push(Piece { color: DEFAULT, matrix: IDENTITY, code: format!("cube({});", i) });
        }
        for _ in 0..50 {
            ps.push(Piece { color: DEFAULT, matrix: IDENTITY, code: "sphere(1);".into() });
        }
        let out = bundle(ps, 10);
        let unique: std::collections::HashSet<_> = out.iter().map(|p| p.code.clone()).collect();
        assert!(unique.len() <= 10, "{}", unique.len());
        assert!(out.iter().filter(|p| p.code == "sphere(1);").count() == 50);
    }
    #[test]
    fn strings_and_modifiers() {
        assert_eq!(parse("%color([1,0,0,1]){import(file=\"a{b}.stl\");}").unwrap().len(), 1);
        assert!(pieces("%color([1,0,0,1]){cube(1);}").is_empty());
    }
    #[test]
    fn import_bytes_invalidate_hash() {
        let t = tempfile::tempdir().unwrap();
        fs::write(t.path().join("x.stl"), b"old").unwrap();
        let a = digest("import(file=\"x.stl\");", "test", t.path()).unwrap();
        fs::write(t.path().join("x.stl"), b"new").unwrap();
        assert_ne!(a, digest("import(file=\"x.stl\");", "test", t.path()).unwrap());
    }
}
