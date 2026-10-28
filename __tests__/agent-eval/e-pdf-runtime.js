// Read an existing optional interpreter; no sync/install and no inherited account settings.
import {execFileSync} from 'node:child_process';
import {readFileSync,realpathSync} from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {root} from './server.js';
export function pdfRuntime() {
  const candidates=process.env.EVAL_PDF_PYTHON ? [process.env.EVAL_PDF_PYTHON] :
    [path.join(root,'.venv/bin/python'),path.resolve(root,'../zlibrary-mcp/.venv/bin/python')];
  for(const executable of candidates) {
    if(!path.isAbsolute(executable)) continue;
    try {
      const details=JSON.parse(execFileSync(executable,['-B','-c',
        'import fitz,json;print(json.dumps(dict(version=fitz.VersionBind,module=fitz.__file__)))'],
        {env:{PATH:process.env.PATH,PYTHONNOUSERSITE:'1',PYTHONDONTWRITEBYTECODE:'1'},encoding:'utf8',stdio:['ignore','pipe','pipe']}));
      const binary=realpathSync(executable);
      return {executable,binary,binarySha256:createHash('sha256').update(readFileSync(binary)).digest('hex'),...details};
    } catch { /* Try the next existing interpreter, never install. */ }
  }
  throw new Error('E14/E15 require an existing PyMuPDF interpreter; set EVAL_PDF_PYTHON');
}
