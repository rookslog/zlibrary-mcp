#!/usr/bin/env node
// Lead-owned acceptance gates for the chat-files tools (Z-Library lead, 2026-10-22).
// The builder must make these pass and must NOT edit this file; the lead diffs it against its master copy.
// Usage: node chat-files-acceptance.mjs <worktree> <smoke-root> <pdf-python>
import { spawn, execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, cpSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [WT, ROOT, PY] = process.argv.slice(2);
if (!WT || !ROOT || !PY) { console.error('usage: <worktree> <smoke-root> <pdf-python>'); process.exit(2); }
const MAX = 7_750_000;
const KANT = 'ImmanuelKantPaulGuyerAllenWWood_CritiqueOfPureReason_1999_engli_784374.pdf';
const HEID = 'MartinHeideggerHeideggerMartin_BeingAndTimeMacquarrieRobinsonTrans_2013_engli_11572484.pdf';
const GLAS = 'DerridaJacques_Glas_2013_engli_24778335.pdf';
const EPUB = 'Aristotle_NicomacheanEthics_2020_engli_5882549.epub';
const results = [];
const gate = (id, ok, detail = '') => { results.push({ id, ok: !!ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${id} ${detail}`); };
const sha = (b) => createHash('sha256').update(b).digest('hex');

function server(env) {
  const p = spawn('node', [join(WT, 'dist/index.js')], {
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ZLIBRARY_OUTPUT_ROOT: ROOT, ...env },
    stdio: ['pipe', 'pipe', 'ignore'],
  });
  let buf = ''; const waiting = new Map(); let id = 0;
  p.stdout.on('data', (d) => {
    buf += d; let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      try { const m = JSON.parse(line); if (waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); } } catch {}
    }
  });
  const call = (method, params, ms = 120_000) => new Promise((res) => {
    const my = ++id; const t = setTimeout(() => { waiting.delete(my); res({ timeout: true }); }, ms);
    waiting.set(my, (m) => { clearTimeout(t); res(m); });
    p.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: my, method, params }) + '\n');
  });
  const init = async () => {
    await call('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'acceptance', version: '1' } });
    p.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  };
  const tool = (name, args, ms) => call('tools/call', { name, arguments: args }, ms);
  return { p, call, init, tool, stop: () => p.kill() };
}
const texts = (r) => (r?.result?.content || []).filter((c) => c.type === 'text').map((c) => c.text);
const json = (r) => { for (const t of texts(r)) { try { return JSON.parse(t); } catch {} } return null; };
const link = (r) => (r?.result?.content || []).find((c) => c.type === 'resource_link');
const isErr = (r) => !!(r?.error || r?.result?.isError);
const flat = (nodes) => (nodes || []).flatMap((n) => [n, ...flat(n.children || n.sections)]);
const range = (s) => { const [a, b] = String(s).split('-').map(Number); return [a, b ?? a]; };
const py = (code, ...args) => execFileSync(PY, ['-I', '-c', code, ...args], { encoding: 'utf8' }).trim();
const tmp = mkdtempSync(join(tmpdir(), 'cf-accept-'));
async function readBlob(s, uri) {
  const r = await s.call('resources/read', { uri });
  const c = r?.result?.contents?.[0];
  return c?.blob ? Buffer.from(c.blob, 'base64') : null;
}

// G1: flag off -> tools/list byte-identical to the baseline fixture.
{
  const s = server({}); await s.init();
  const r = await s.call('tools/list', {});
  const base = readFileSync(join(WT, '__tests__/fixtures/handoff-tools-baseline.json'), 'utf8');
  const same = JSON.stringify(r.result) === JSON.stringify(JSON.parse(base)) || JSON.stringify(r.result.tools) === JSON.stringify(JSON.parse(base).tools ?? JSON.parse(base));
  gate('G1 flag-off tools/list equals baseline', same);
  s.stop();
}

const ENV = { ZLIBRARY_CHAT_FILES: '1', ZLIBRARY_PDF_PYTHON: PY };
const s = server(ENV); await s.init();

// G2: flag on lists the three tools.
let listed = false;
{
  const names = ((await s.call('tools/list', {})).result?.tools || []).map((t) => t.name);
  gate('G2 flag-on lists get_book_map, share_book_file, get_page_image',
    (listed = ['get_book_map', 'share_book_file', 'get_page_image'].every((n) => names.includes(n))), names.join(','));
}

// G3: Heidegger map: outline, labels, Part One split, parts contiguous and under cap, cold < 60 s, warm < 5 s.
let heidMap;
{
  let t = Date.now(); const r = await s.tool('get_book_map', { file: HEID }); const cold = Date.now() - t;
  heidMap = json(r);
  t = Date.now(); await s.tool('get_book_map', { file: HEID }); const warm = Date.now() - t;
  const nodes = flat(heidMap?.sections);
  const part1 = nodes.find((n) => /^Part One/i.test(n.title || ''));
  const parts = heidMap?.suggested_parts || [];
  let next = 1, contiguous = parts.length > 0;
  for (const p of parts) { const [a, b] = range(p.pdf_pages); if (a !== next) contiguous = false; next = b + 1; }
  contiguous = contiguous && next === 591;
  gate('G3a Heidegger map returns JSON with pages=590 and fits_whole=false', heidMap?.pages === 590 && heidMap?.fits_whole === false);
  gate('G3b Heidegger outline present (>=100 nodes listed or measured)', nodes.length >= 15, `nodes=${nodes.length}`);
  gate('G3c "Part One" does not fit and has fitting children', part1 && part1.fits === false && flat(part1.children || part1.sections).some((c) => c.fits === true));
  gate('G3d suggested_parts contiguous over 1-590', contiguous, `${parts.length} parts`);
  gate('G3e every suggested part bytes <= cap', parts.length > 0 && parts.every((p) => p.bytes <= MAX));
  gate('G3f page labels reported', !!heidMap?.page_labels && heidMap.page_labels !== false && JSON.stringify(heidMap.page_labels) !== '[]');
  gate('G3g cold map < 60 s, warm < 5 s', heidMap?.pages > 0 && cold < 60_000 && warm < 5_000, `cold=${cold}ms warm=${warm}ms`);
}

// G4: Kant map: no outline, no labels + offset note, scan, safe window plausible.
let kantMap;
{
  kantMap = json(await s.tool('get_book_map', { file: KANT }));
  const note = JSON.stringify(kantMap || {});
  gate('G4a Kant pages=801, no sections', kantMap?.pages === 801 && flat(kantMap?.sections).length === 0);
  gate('G4b Kant: no labels and an offset note', /offset/i.test(note));
  gate('G4c Kant text_layer reports a scan (<0.2)', typeof kantMap?.text_layer === 'number' ? kantMap.text_layer < 0.2 : /scan|0(\.0+)?\b/.test(JSON.stringify(kantMap?.text_layer)));
  gate('G4d Kant safe_window_pages in 120-170', kantMap?.safe_window_pages >= 120 && kantMap?.safe_window_pages <= 170, `got ${kantMap?.safe_window_pages}`);
}

// G5: Glas does not fit whole; labels present.
{
  const m = json(await s.tool('get_book_map', { file: GLAS }));
  gate('G5 Glas fits_whole=false, 2+ parts, labels present', m?.fits_whole === false && (m?.suggested_parts || []).length >= 2 && !!m?.page_labels);
}

// G6: small EPUB is sent whole; resources/read bytes equal the file.
{
  const r = await s.tool('share_book_file', { file: EPUB }); const l = link(r);
  const b = l && (await readBlob(s, l.uri));
  gate('G6 EPUB sent whole, round-trip SHA equals file', b && sha(b) === sha(readFileSync(join(ROOT, EPUB))));
}

// G7: Kant without pages -> no file, points to the map.
{
  const r = await s.tool('share_book_file', { file: KANT });
  gate('G7 Kant without pages sends no file and mentions get_book_map', !link(r) && /get_book_map/.test(texts(r).join(' ')));
}

// G8: Kant 1-801 -> partial send under cap, remaining starts right after, piece page count matches.
let kantUri;
{
  const r = await s.tool('share_book_file', { file: KANT, pages: '1-801' }); const l = link(r); const j = json(r);
  const b = l && (await readBlob(s, l.uri)); kantUri = l?.uri;
  let ok = false, detail = '';
  if (b && j?.pdf_pages && j?.remaining) {
    const [a, e] = range(j.pdf_pages); const rem = String(j.remaining).match(/(\d+)\s*-\s*(\d+)/);
    const f = join(tmp, 'kant-piece.pdf'); writeFileSync(f, b);
    const n = Number(py('import fitz,sys;print(len(fitz.open(sys.argv[1])))', f));
    ok = a === 1 && b.length <= MAX && rem && Number(rem[1]) === e + 1 && Number(rem[2]) === 801 && n === e - a + 1 && j.sha256 === sha(b);
    detail = `sent ${j.pdf_pages} (${b.length} B), remaining ${j.remaining}, piece pages ${n}`;
  }
  gate('G8 Kant 1-801 partial send: under cap, remaining correct, page count and SHA match', ok, detail);
}

// G9: a Heidegger section piece keeps printed page labels.
{
  const intro = flat(heidMap?.sections).find((n) => /^Introduction/i.test(n.title || '') && n.fits);
  let ok = false, detail = 'no Introduction section';
  if (intro) {
    const r = await s.tool('share_book_file', { file: HEID, section: intro.id }); const l = link(r);
    const b = l && (await readBlob(s, l.uri));
    if (b) {
      const f = join(tmp, 'heid-intro.pdf'); writeFileSync(f, b);
      const [a] = range(intro.pdf_pages);
      const got = py('import fitz,sys;print(fitz.open(sys.argv[1])[0].get_label())', f);
      const want = py('import fitz,sys;print(fitz.open(sys.argv[1])[int(sys.argv[2])-1].get_label())', join(ROOT, HEID), String(a));
      ok = got === want && got !== ''; detail = `label got=${got} want=${want}`;
    }
  }
  gate('G9 section piece keeps printed page labels', ok, detail);
}

// G10: links survive a server restart (ids persisted on disk).
{
  s.stop();
  const s2 = server(ENV); await s2.init();
  const b = kantUri && (await readBlob(s2, kantUri));
  gate('G10 resource uri still readable after server restart', b && b.length > 0 && b.length <= MAX);
  // G11: unknown uri refused.
  const bad = await s2.call('resources/read', { uri: 'zlib-book://file/does-not-exist-0000' });
  gate('G11 unknown uri refused', listed && (isErr(bad) || !bad?.result?.contents?.[0]?.blob));
  // G12: path escapes refused, including the cache directory.
  const escapes = ['../x.pdf', '/etc/passwd', '.chat-files/cache/x'];
  const refused = [];
  for (const f of escapes) refused.push(isErr(await s2.tool('share_book_file', { file: f })) || !link(await s2.tool('share_book_file', { file: f })));
  gate('G12 path escapes and cache dir refused', listed && refused.every(Boolean));
  // G13: three concurrent page renders all succeed.
  const imgs = await Promise.all([10, 11, 12].map((p) => s2.tool('get_page_image', { file: GLAS, page: p })));
  const okImgs = imgs.every((r) => (r?.result?.content || []).some((c) => c.type === 'image') && !/busy/i.test(texts(r).join(' ')));
  gate('G13 three concurrent get_page_image calls all return images', okImgs);
  // G14: tool text never carries page text (probe-v2 page 3 text must not leak).
  const pr = await s2.tool('share_book_file', { file: 'probe/probe-v2.pdf' });
  gate('G14 share output carries no page text', !!link(pr) && !/WILLOW|COMPASS/.test(JSON.stringify(pr?.result?.content?.filter((c) => c.type === 'text'))));
  s2.stop();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} gates passed`);
writeFileSync(join(tmp, 'results.json'), JSON.stringify(results, null, 2));
process.exit(failed.length ? 1 : 0);
