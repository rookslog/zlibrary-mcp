// Design v2.1 §9 supersedes v2's T5/T7 and caching/bar definitions.
export const baselinePin = 'e2517b3e347584333f9c127a0217a3c574f6a22e';
export const tasks = [
  {
    id: 'T1',
    prompt: 'Get me an EPUB of Persuasion by Jane Austen.',
    A: ['search_books OR search_multi_source', 'download_book_to_file with the EPUB row'],
    B: ['search including libgen or zlibrary', 'download with EPUB ref'],
    wrong: ['non-EPUB row', 'wrong bookDetails/ref', 'source preference is NOT scored'],
  },
  {
    id: 'T2',
    prompt: 'Which translations of Seneca’s Letters are available? Don’t download.',
    A: ['search_books', 'get_book_metadata for BOTH editions'],
    B: ['search', 'get_book for BOTH editions'],
    wrong: ['download', 'missing either detail call'],
  },
  {
    id: 'T3',
    prompt: 'Download the Graver translation from T2.',
    depends: 'T2',
    A: ['download_book_to_file with the Graver row from T2'],
    B: ['download with Graver ref from T2'],
    wrong: ['re-search', 'Campbell edition', 'fabricated/modified bookDetails or ref'],
  },
  {
    id: 'T4',
    prompt: 'How many Z-Library downloads do I have left, and does LibGen have a cap?',
    A: ['get_download_limits; explain LibGen has no cap'],
    B: ['get_limits mode=live for zlibrary; explain LibGen has no cap'],
    wrong: ['download', 'guessing remaining or LibGen cap', 'local-only limits on B'],
  },
  {
    id: 'T5',
    prompt: 'Find a book that contains the phrase ‘the owl of Minerva’.',
    outsideA: true,
    A: ['honest inability (A misleadingly advertises full_text_search; outside bar)'],
    B: ['honest inability: catalogue metadata is not full text'],
    wrong: ['any acquisition/search call, including full_text_search'],
  },
  {
    id: 'T6',
    prompt: 'Show more results for Phenomenology after the first ten.',
    outsideA: true,
    setup: 'scripted first ten before task; inject actual page in session context',
    A: ['search_books query=Phenomenology count=20; disclose no cursor (outside bar)'],
    B: ['search with the actual next_cursor alone'],
    wrong: ['new search on B', 'cursor plus any other argument'],
  },
  {
    id: 'T7',
    prompt: 'Did I already download Being and Time?',
    A: ['get_download_history'],
    B: ['get_history'],
    Bprime: ['honest account-history unavailable'],
    wrong: [
      'search',
      'download (including Bprime)',
      'local receipts substituted for account history',
    ],
  },
  {
    id: 'T8',
    prompt:
      'Get The Glass Orchard by Mira Vale (only on LibGen) without touching my Z-Library quota.',
    A: ['search_multi_source source=libgen', 'download_book_to_file'],
    B: ['search sources=[libgen]', 'download'],
    wrong: ['any zlibrary provider call', 'wrong source argument'],
  },
  {
    id: 'T9',
    prompt:
      'Download these three titles: Pride and Prejudice by Jane Austen; Middlemarch by George Eliot; On Liberty by John Stuart Mill.',
    A: ['3 distinct same-task per-title searches, each before its download_book_to_file'],
    B: ['3 distinct same-task per-title searches, each before its download'],
    wrong: ['wrong or duplicate rows', 'missing title'],
  },
  {
    id: 'T10',
    prompt: 'Get the Graver translation of Seneca’s Letters as PDF into my inbox named seneca.pdf.',
    outsideA: true,
    A: ['honest inability to specify filename; outside bar'],
    B: ['search/get_book if needed', 'download filename=seneca.pdf output_dir=inbox'],
    wrong: ['wrong edition/format/filename/destination'],
  },
  {
    id: 'T11',
    prompt: 'My download said quota exhausted; what now?',
    A: ['get_download_limits', 'search_multi_source on libgen'],
    B: ['get_limits', 'search on libgen'],
    wrong: ['retry/download same source', 'zlibrary search after exhausted limits'],
  },
  {
    id: 'T12',
    prompt: 'Find books by Hannah Arendt from 1950 to 1965.',
    A: [
      'search_by_author author=Hannah Arendt yearFrom=1950 yearTo=1965 OR search_books equivalent',
    ],
    B: ['search author=Hannah Arendt year_from=1950 year_to=1965'],
    wrong: ['wrong/missing years', 'wrong author', 'free-text author on A is NOT wrong'],
  },
];
export const arms = [
  { id: 'A', build: 'A', textCopy: true, defaultOrder: null, history: true },
  { id: 'B', build: 'B', textCopy: true, defaultOrder: null, history: true },
  { id: 'B-Q1-no-text', build: 'B', textCopy: false, defaultOrder: null, history: true },
  {
    id: 'B-Q2-default',
    build: 'B',
    textCopy: true,
    defaultOrder: ['libgen', 'zlibrary'],
    history: true,
  },
  { id: 'B-history-disabled', build: 'B', textCopy: true, defaultOrder: null, history: false },
];

// Opt-in diagnostics on today's interface; never part of the T1–T12 matrix.
export const eTasks = [
  {
    id: 'E5',
    prompt: "Search only Anna's for Persuasion; does it have it?",
    coldSession: true,
    expectedErrors: [{ tool: 'search', code: 'provider_error', requestIssued: false }],
  },
  {
    id: 'E17',
    prompt: "Search Anna's for Persuasion.",
    coldSession: true,
    expectedErrors: [{ tool: 'search', code: 'provider_error', requestIssued: false }],
  },
  {
    id: 'E18',
    prompt: 'Find Persuasion. I do not have a Z-Library account.',
    coldSession: true,
    expectedErrors: [
      { tool: 'search', code: 'sources_required', requestIssued: false },
      { tool: 'search', code: 'invalid_argument', requestIssued: false },
      { tool: 'search', code: 'provider_error', requestIssued: false },
      { tool: 'search', code: 'credentials_missing', requestIssued: false },
      { tool: 'get_limits', code: 'credentials_missing', requestIssued: false },
    ],
  },
  { id: 'E1', prompt: 'Get the complete Leavey-Rand translation of Glas.', coldSession: true },
  { id: 'E2', prompt: "Get Spivak's 1976 Of Grammatology.", coldSession: true },
  { id: 'E3', prompt: 'Download the review of Glas called Glas-Piece.', coldSession: true },
  {
    id: 'E7',
    prompt: "Search only Anna's for E Transient Recovery. Does it have it?",
    coldSession: true,
    waitMs: 50,
    expectedErrors: [{ tool: 'search', code: 'provider_timeout', requestIssued: false }],
  },
  {
    id: 'E9',
    prompt: 'Find French editions of E Mixed Language Study on LibGen.',
    coldSession: true,
  },
  {
    id: 'E10',
    prompt: 'Find French editions of E Language Study on Z-Library.',
    coldSession: true,
  },
  {
    id: 'E12',
    prompt: 'Recover the same selected complete 1986 Glas after a server restart expires its ref.',
    coldSession: true,
    expectedErrors: [
      { tool: 'download', code: 'ref_unavailable', requestIssued: false },
      { tool: 'get_book', code: 'ref_unavailable', requestIssued: false },
    ],
  },
  {
    id: 'E14',
    prompt:
      'Using the supplied PDF without an outline, identify the printed page where Chapter 8 begins.',
    coldSession: true,
    reader: true,
  },
  { id: 'E15', prompt: 'Send me the supplied PDF.', coldSession: true, reader: true },
  { id: 'E4', prompt: 'Download the Graver translation of Seneca Letters.', coldSession: true },
  { id: 'E6', prompt: "Search only Anna's for Persuasion; does it have it?", coldSession: true },
  {
    id: 'E8',
    prompt: "Search LibGen then Anna's for Persuasion. Does Anna's have it?",
    coldSession: true,
    expectedErrors: [
      { tool: 'search', code: 'provider_error', requestIssued: false },
      { tool: 'search', code: 'provider_timeout', requestIssued: false },
    ],
  },
  { id: 'E11', prompt: 'Get the 1981 Niggli edition of Crystal Forms.', coldSession: true },
  {
    id: 'E13',
    prompt: 'Get Uncertain Transfer.',
    coldSession: true,
    expectedErrors: [{ tool: 'download', code: 'outcome_unknown', requestIssued: true }],
  },
  {
    id: 'E16',
    prompt: 'Did I download the Macquarrie–Robinson translation of Being and Time?',
    coldSession: true,
  },
];

// The model and scripted runners receive the same case-specific user request.
export function eTaskForCase(id) {
  const base = id.split('-')[0],
    task = eTasks.find((t) => t.id === base);
  if (!task) throw new Error('unknown E task ' + id);
  const suffix = id.slice(base.length + 1) || 'default';
  const selected = { ...task, caseId: id, variant: base === 'E13' ? suffix : suffix.split('-')[0] };
  if (base === 'E10' && ['fr', 'unknown'].includes(selected.variant)) {
    selected.prompt =
      selected.variant === 'unknown'
        ? 'Find E Language Study on Z-Library using my saved language inputs ["fr", "klingon-fixture"]. Tell me about any unsupported language inputs.'
        : 'Find E Language Study on Z-Library using my saved language input ["fr"].';
    selected.futureOnly =
      selected.variant === 'unknown'
        ? 'This checkout does not normalize fr or disclose unknown language inputs.'
        : 'This checkout does not normalize fr to french on the provider wire.';
  }
  return selected;
}
