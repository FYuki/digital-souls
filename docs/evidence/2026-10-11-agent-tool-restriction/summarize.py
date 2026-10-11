import json,sys
f=sys.argv[1]
for l in open(f):
    d=json.loads(l); t=d.get('type')
    if t=='system' and d.get('subtype')=='init': print('perm',d.get('permissionMode'),'apiKeySource',d.get('apiKeySource')); print('tools',d.get('tools')); print('mcp',[(m['name'],m['status']) for m in d.get('mcp_servers',[])])
    if t=='system' and d.get('subtype','').startswith('hook'): print('HOOK',d.get('subtype'),d.get('hook_name'))
    if t=='assistant':
        for c in d['message']['content']:
            if c['type']=='tool_use': print('TOOL_USE',c['name'],json.dumps(c['input'])[:80])
    if t=='user':
        for c in d['message']['content']:
            if isinstance(c,dict) and c.get('type')=='tool_result': print('RESULT',str(c.get('content'))[:100],c.get('is_error'))
    if t=='result': print('FINAL',d.get('subtype'),str(d.get('result'))[:400].replace('\n',' '))
