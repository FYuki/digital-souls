'use strict';
const { python } = require('./bridge.cjs');
function assertion(output, context) {
  try {
    const parsed = typeof output === 'string' ? JSON.parse(output) : output;
    const observed = parsed.observation || parsed;
    if (Object.keys(context.vars).join() !== 'case_id') throw new Error();
    const checked = python({operation:'score', id:context.vars.case_id, observation:observed});
    return {pass:checked.passed, score:checked.passed ? 1 : 0, reason:'deterministic-answer',
      metadata:checked, assertion:{type:'javascript',metric:'core-answer',value:`file://${__filename}`}};
  } catch {
    return {pass:false, score:0, reason:'invalid-answer-output'};
  }
}
module.exports = assertion;
