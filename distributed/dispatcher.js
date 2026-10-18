export default {
 async fetch(request, env) {
  const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{"content-type":"application/json","cache-control":"no-store"}});
  if(request.method==="GET" && new URL(request.url).pathname==="/health") return json({ok:true,service:"yct-dispatcher"});
  if(!env.DISPATCH_TOKEN || request.headers.get("authorization")!=="Bearer "+env.DISPATCH_TOKEN) return json({error:"unauthorized"},401);
  const url=new URL(request.url), path=url.pathname;
  try {
   if(path==="/jobs" && request.method==="POST"){
    const b=await request.json();
    if(!Number.isSafeInteger(b.book_id)||b.book_id<=0||!Number.isSafeInteger(b.start_page)||!Number.isSafeInteger(b.end_page)||b.start_page<1||b.end_page<b.start_page||b.end_page-b.start_page>199||typeof b.title!=="string") return json({error:"invalid job"},400);
    const id=crypto.randomUUID();
    await env.DB.prepare("INSERT INTO jobs(id,book_id,title,start_page,end_page,status) VALUES(?,?,?,?,?,'queued')").bind(id,b.book_id,b.title.slice(0,200),b.start_page,b.end_page).run();
    return json({id,status:"queued"},201);
   }
   if(path==="/claim" && request.method==="POST"){
    const b=await request.json(), owner=String(b.owner||"").slice(0,100);
    if(!owner) return json({error:"owner required"},400);
    const lease=Date.now()/1000|0;
    const res=await env.DB.prepare("UPDATE jobs SET status='running',lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=unixepoch() WHERE id=(SELECT id FROM jobs WHERE (status='queued' OR (status='running' AND lease_until<unixepoch())) AND attempts<5 ORDER BY created_at LIMIT 1) RETURNING *").bind(owner,lease+180,).first();
    return json({job:res||null});
   }
   const m=path.match(/^\/jobs\/([0-9a-f-]{36})(?:\/(heartbeat|complete|fail))?$/);
   if(m){
    const id=m[1],action=m[2];
    if(request.method==="GET"&&!action){const row=await env.DB.prepare("SELECT id,book_id,title,start_page,end_page,status,attempts,result,updated_at FROM jobs WHERE id=?").bind(id).first();return json({job:row||null},row?200:404);}
    if(request.method==="POST"&&action){
     const b=await request.json(),owner=String(b.owner||"");
     if(!owner) return json({error:"owner required"},400);
     const sql=action==="heartbeat"?"UPDATE jobs SET lease_until=unixepoch()+180,updated_at=unixepoch() WHERE id=? AND status='running' AND lease_owner=? AND lease_until>=unixepoch() RETURNING id":action==="complete"?"UPDATE jobs SET status='done',result=?,lease_until=0,updated_at=unixepoch() WHERE id=? AND status='running' AND lease_owner=? AND lease_until>=unixepoch() RETURNING id":"UPDATE jobs SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'queued' END,result=?,lease_until=0,updated_at=unixepoch() WHERE id=? AND status='running' AND lease_owner=? AND lease_until>=unixepoch() RETURNING id";
     const stmt=env.DB.prepare(sql);
     const row=action==="heartbeat"?await stmt.bind(id,owner).first():await stmt.bind(JSON.stringify(b.result||{}).slice(0,2000),id,owner).first();
     return json({ok:!!row},row?200:409);
    }
   }
   return json({error:"not_found"},404);
  } catch(e){return json({error:"server_error"},500);}
 }
};