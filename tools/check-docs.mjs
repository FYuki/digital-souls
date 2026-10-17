import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

export function checkManifest(root, manifest) {
  const errors = [];
  for (const entry of manifest.files) {
    if (entry.status !== 'included') {
      if (entry.path) errors.push('Excluded asset must not have a local path');
      continue;
    }
    const full = resolve(root, entry.path);
    const rel = relative(root, full);
    if (isAbsolute(entry.path) || rel === '..' || rel.startsWith('../') || rel.startsWith('..\\')) {
      errors.push('Asset path escapes sample directory');
      continue;
    }
    if (!existsSync(full)) { errors.push(`Missing asset: ${entry.path}`); continue; }
    const bytes = readFileSync(full);
    if (bytes.length !== entry.bytes || createHash('sha256').update(bytes).digest('hex') !== entry.sha256) {
      errors.push(`Asset hash/size mismatch: ${entry.path}`);
    }
  }
  return errors;
}

export function checkText(root, file, text) {
  const errors = [];
  if (text.includes('\r')) errors.push(`${file}: use LF newlines`);
  if (!text.endsWith('\n')) errors.push(`${file}: missing final newline`);
  if (/[^\S\n]+$/m.test(text)) errors.push(`${file}: trailing whitespace`);
  if (file.endsWith('.json')) {
    try { JSON.parse(text); } catch { errors.push(`${file}: invalid JSON`); }
  }
  if (file.endsWith('.md')) {
    // Deliberately limited to inline Markdown links, including images.
    for (const match of text.matchAll(/\[[^\]]*\]\(([^\s)]+)\)/g)) {
      const target = match[1];
      if (/^(?:[a-z][a-z\d+.-]*:|#)/i.test(target)) continue;
      let path;
      try { path = decodeURIComponent(target.split(/[?#]/)[0]); }
      catch { errors.push(`${file}: invalid link encoding ${target}`); continue; }
      const full = resolve(root, dirname(file), path);
      const rel = relative(root, full);
      if (isAbsolute(path) || rel === '..' || rel.startsWith('../') || rel.startsWith('..\\')) {
        errors.push(`${file}: link escapes repository ${target}`);
      } else if (!existsSync(full)) {
        errors.push(`${file}: missing local target ${target}`);
      }
    }
  }
  return errors;
}

export function checkRepository(root) {
  const files = execFileSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' })
    .split('\0').filter(Boolean)
    .filter(file => /\.(md|mjs|json|yml|yaml)$/.test(file) ||
      ['.node-version', '.gitattributes', '.gitignore'].includes(file));
  if (files.length === 0) throw new Error('No tracked documentation/config files');
  const errors = files.flatMap(file => checkText(root, file, readFileSync(resolve(root, file), 'utf8')));
  const manifests = files.filter(file => file.startsWith('core/characters/') && file.endsWith('/manifest.json'));
  if (manifests.length === 0 && files.some(file => file.startsWith('core/characters/'))) {
    errors.push('core/characters: no sample manifest found');
  }
  for (const file of manifests) {
    try {
      errors.push(...checkManifest(resolve(root, dirname(file)), JSON.parse(readFileSync(resolve(root, file), 'utf8'))));
    } catch (error) { errors.push(`${file}: invalid manifest: ${error.message}`); }
  }
  return errors;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const errors = checkRepository(process.cwd());
  if (errors.length) {
    console.error(errors.join('\n'));
    process.exitCode = 1;
  } else {
    console.log('PASS: tracked text and local Markdown file targets (not product tests)');
  }
}
