import argparse,json,pathlib,subprocess,time
parser=argparse.ArgumentParser();parser.add_argument("--root",required=True);parser.add_argument("--binary",required=True);parser.add_argument("--label",required=True);parser.add_argument("--cases",required=True)
args=parser.parse_args();root=pathlib.Path(args.root);logs=root/("logs-"+args.label);logs.mkdir(exist_ok=True)
cases=json.loads(pathlib.Path(args.cases).read_text())
for i,case in enumerate(cases):
 name="sub2api-mem-"+args.label+"-"+str(i)
 command=["docker","run","--name",name,"--network","none","--user","0:0","--memory","768m","--memory-swap","768m","--cpus","2","-v",str(root)+":/measure:ro","--entrypoint","/measure/"+args.binary]
 env={"GOMEMLIMIT":"512MiB","GOGC":str(case.get("gogc",100)),"GOMAXPROCS":"2","SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE":"1","SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE":case["shape"],"SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE":"/measure/fixtures/"+case["fixture"],"SUB2API_OFFICIAL_EGRESS_MEMORY_CONCURRENCY":str(case.get("concurrency",1)),"SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT":str(case.get("requests",1)),"SUB2API_OFFICIAL_EGRESS_MEMORY_STAGE":case.get("stage","forward"),"SUB2API_OFFICIAL_EGRESS_MEMORY_INGRESS_ENCODING":case.get("encoding","identity")}
 if case.get("retry_first"):env["SUB2API_OFFICIAL_EGRESS_MEMORY_RETRY_FIRST"]="1"
 if case.get("wire"):env["SUB2API_OFFICIAL_EGRESS_MEMORY_WIRE_FIXTURE"]="/measure/fixtures/"+case["wire"]
 if case.get("ws_fixture"):env["SUB2API_OFFICIAL_EGRESS_MEMORY_WS_FIXTURE"]="/measure/fixtures/"+case["ws_fixture"]
 if case.get("ws_session_fixture_dir"):env["SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR"]="/measure/ws-fixtures/"+case["ws_session_fixture_dir"]
 for key,value in env.items():command.extend(["-e",key+"="+value])
 command.extend(["ghcr.io/itv3/sub2apiplus:0.2.13-2","-test.v","-test.run=^"+case.get("test","TestOfficialEgressHTTPForwardMemoryProfile")+"$"])
 started=time.time()
 try:
  result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=180)
  (logs/(case["name"]+".log")).write_text(result.stdout)
  inspect=subprocess.run(["docker","inspect",name,"--format","{{json .State}}"],capture_output=True,text=True)
  state=json.loads(inspect.stdout) if inspect.returncode==0 else {"inspection_error":inspect.stderr}
  rows=[]
  for line in result.stdout.splitlines():
   if "MEMPROFILE_RESULT " in line:
    row=json.loads(line.split("MEMPROFILE_RESULT ",1)[1]);row.update(measurement_label=args.label,case=case,container_state=state,container_memory_limit_bytes=768<<20,command=command);rows.append(row)
  if not rows:rows=[dict(measurement_label=args.label,case=case,container_state=state,error=result.stdout[-4000:])]
  with (root/(args.label+"-results.jsonl")).open("a") as output:
   for row in rows:output.write(json.dumps(row,ensure_ascii=False)+"\n")
  for row in rows:
   print(json.dumps({"name":case["name"],"exit_code":result.returncode,"oom_killed":state.get("OOMKilled"),**{key:row.get(key) for key in ["resident_multiple_with_body","heap_request_peak_mib","request_memory_peak_mib","owned_memory_peak_mib","owned_memory_after_release_bytes","rss_peak_mib","cgroup_peak_mib","elapsed_ms","cpu_ms","large_body_target_met","heap_after_release_mib"]}},ensure_ascii=False),flush=True)
 finally:subprocess.run(["docker","rm","-f",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
