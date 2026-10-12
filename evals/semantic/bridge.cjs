'use strict';
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
function python(request, environment = {}) {
  const child = spawnSync(path.join(root, '.venv/bin/python'),
    ['-m', 'digital_souls_core.semantic_answer_bridge'], {
      cwd: root, input: JSON.stringify(request), encoding: 'utf8', timeout: request.operation === 'dry-run' ? 600000 : 30000,
      maxBuffer: 16 * 1024 * 1024,
      env: { ...environment, PATH: process.env.PATH, LANG: 'C.UTF-8', LITELLM_LOCAL_MODEL_COST_MAP: 'True' },
    });
  if (child.status !== 0) throw new Error('Answer scoring bridge failed');
  return JSON.parse(child.stdout);
}
module.exports = { python };
