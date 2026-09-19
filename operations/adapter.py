import json

def serve(adapter, stdin, stdout):
    seen=set()
    for line in stdin:
        try:
            req=json.loads(line)
            rid=req.get('id')
            if rid is None or req.get('jsonrpc') != '2.0': raise ValueError('invalid JSON-RPC request')
            if rid in seen: raise ValueError('duplicate request id')
            seen.add(rid)
            args=req.get('params',{}).get('arguments',{})
            result=adapter.call(args.get('name'), args.get('input') or {})
            stdout.write(json.dumps({'jsonrpc':'2.0','id':rid,'result':result},ensure_ascii=False)+'\n')
        except Exception as exc:
            stdout.write(json.dumps({'jsonrpc':'2.0','id':locals().get('rid'),'error':{'code':-32600,'message':str(exc)}},ensure_ascii=False)+'\n')
