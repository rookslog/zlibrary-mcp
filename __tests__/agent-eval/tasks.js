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
    B: ['search sources=[libgen]', 'get_book if copy-specific metadata is needed', 'download'],
    wrong: ['any zlibrary provider call', 'wrong source argument'],
  },
  {
    id: 'T9',
    prompt:
      'Download these three titles: Pride and Prejudice by Jane Austen; Middlemarch by George Eliot; On Liberty by John Stuart Mill.',
    A: ['3 distinct same-task per-title searches, each before its download_book_to_file'],
    B: ['3 distinct same-task per-title searches, optional copy-specific get_book, each before its download'],
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
    B: ['get_limits', 'search sources=[libgen], or omit only when reported defaults exclude the exhausted source'],
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
