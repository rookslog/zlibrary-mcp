import { expect, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';

const env = { ZLIBRARY_EMAIL: 'offline@example.invalid', ZLIBRARY_PASSWORD: 'password' };

test.each(['zlibrary', 'libgen', 'annas'])(
  '%s search normalizes unknown years and page counts while retaining raw records',
  async (source) => {
    // Gate G rows 3 and 4, plus string/empty/missing sentinels and known values.
    const metadata = [
      { year: 1996, pages: 0 },
      { year: 0, pages: 0 },
      { year: '0', pages: '0' },
      { year: '', pages: '' },
      { year: '  ', pages: '  ' },
      { year: null, pages: null },
      {},
      { year: '1981', pages: '184' },
    ];
    const books = metadata.map((fields, i) => ({
      id: String(i + 1),
      hash: 'abc',
      md5: String(i).padStart(32, '0'),
      title: 'Grid systems',
      source,
      ...fields,
    }));
    const tools = new AcquisitionTools(async () => ({ books }), env);
    const result = await tools.call('search', { query: 'grid', sources: [source] });
    expect(result.rows.map(({ year, pages }) => ({ year, pages }))).toEqual([
      { year: 1996, pages: null },
      { year: null, pages: null },
      { year: null, pages: null },
      { year: null, pages: null },
      { year: null, pages: null },
      { year: null, pages: null },
      { year: null, pages: null },
      { year: 1981, pages: 184 },
    ]);
    for (let i = 0; i < books.length; i++) {
      expect(tools.refs.get(result.rows[i].ref, 'r').book).toEqual(books[i]);
    }
  },
);
