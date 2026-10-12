'use strict';
const fs = require('node:fs');
const path = require('node:path');
function configuration(mode, options = {}) {
  if (!['fixture','local_model','external_model'].includes(mode)) throw new Error('Unknown mode');
  if (['local_model','external_model'].includes(mode) && (!options.profile || !path.isAbsolute(options.profile))) {
    throw new Error('NOT RUN: explicit profile required');
  }
  if (mode === 'fixture' && options.profile) throw new Error('Fixture rejects profile');
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname,'cases.json'),'utf8'));
  const ids = cases.cases.map(c => c.id);
  if (!ids.length || new Set(ids).size !== ids.length) throw new Error('Invalid cases');
  const config = {mode, pythonExecutable:path.resolve(__dirname,'../../.venv/bin/python')};
  if (options.profile) config.profile_path = options.profile;
  if (options.variant) config.fixture_variant = options.variant;
  return {
    description:'Core semantic answer evaluation', sharing:false,
    prompts:['{{case_id}}'],
    providers:[{id:`file://${path.join(__dirname,'provider.py')}`,label:'core-answer',config}],
    defaultTest:{options:{disableConversationVar:true,disableVarExpansion:true},
      assert:[{type:'javascript',metric:'core-answer',value:`file://${path.join(__dirname,'assertions.cjs')}`}]},
    tests:ids.map(id => ({description:id,vars:{case_id:id}})),
    evaluateOptions:{maxConcurrency:1},
  };
}
module.exports = { configuration };
if (process.env.DSC_ANSWER_MODE) module.exports = configuration(process.env.DSC_ANSWER_MODE, {
  profile:process.env.DSC_ANSWER_PROFILE,variant:process.env.DSC_ANSWER_VARIANT,
});
