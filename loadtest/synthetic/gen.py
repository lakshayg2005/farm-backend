"""Synthetic load-test metrics (OpenMetrics) shaped like the real farm dashboards expect.
Model: capacity knee ~400 VUs (p95 climbs), saturation ~900 VUs (5xx, timeouts), DB pool 100 conns.
Usage: python gen.py out.om  -> promtool tsdb create-blocks-from openmetrics out.om dir"""
import math, random, sys, time
random.seed(7)
STEP = 15
END = int(time.time()) - 15 * 60
# (minutes, vus) breakpoints: smoke, load, stress steps, spike, recovery, idle
PLAN = [(0,0),(1,5),(4,5),(5,50),(12,50),(13,100),(20,100),(21,200),(28,200),(29,200),
        (30,400),(35,400),(36,700),(41,700),(42,1000),(46,1000),(47,1500),(50,1500),(51,50),
        (57,50),(58,800),(60,800),(61,50),(66,50),(68,0),(72,0)]
N = 72 * 60 // STEP
START = END - N * STEP
def vus_at(m):
    for (a,va),(b,vb) in zip(PLAN, PLAN[1:]):
        if a <= m <= b:
            if b - a <= 1.01 or va == vb: return va + (vb-va)*(m-a)/max(b-a,1e-9)
            return va + (vb-va)*(m-a)/(b-a)
    return 0
CAP_RPS = 520.0; KNEE = 400
SVC = {  # service: [(method, route, share, base_ms, 4xx)]
 'catalog': [('GET','/listings/search',.22,70,.002),('GET','/listings/{id}',.14,35,.01),('GET','/crops',.06,15,0),('POST','/listings',.005,90,.03)],
 'land': [('GET','/plots/search',.07,95,.002),('GET','/plots/{id}',.04,40,.01)],
 'identity': [('POST','/login',.03,210,.04),('POST','/register',.004,260,.05),('GET','/me',.1,18,.005)],
 'order': [('GET','/orders',.08,60,0),('POST','/orders',.03,160,.03)],
 'notification': [('GET','/notifications/unread',.1,12,0)],
 'logistics': [('POST','/pings',.025,25,.01),('GET','/routes/today',.01,55,0)],
}
BOUNDS = [.005,.01,.025,.05,.075,.1,.25,.5,.75,1,2.5,5,7.5,10]
def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def lat_scale(rho):
    if rho < .6: return 1+.4*rho
    if rho < 1: return 1+.4*.6 + 4*(rho-.6)**2/(1.02-rho)*.6
    return 1+ 40*min(rho-.9,2)
out = {}  # name -> list of (labels, [(t,v)])
def put(name, labels, ts, v):
    out.setdefault((name,labels),[]).append((ts,v))
state = {}
def cnt(key, inc): state[key] = state.get(key,0)+inc; return state[key]
iters = 0; reqs_ok = {}; 
for i in range(N+1):
    t = START + i*STEP; m = i*STEP/60
    vus = max(0, vus_at(m)); 
    vus_n = vus * (1+random.gauss(0,.02)) if vus else 0
    demand = vus_n*0.6
    thr = min(demand, CAP_RPS*(1+random.gauss(0,.015)))
    rho = demand/CAP_RPS if demand else 0
    sc = lat_scale(rho)*(1+abs(random.gauss(0,.05)))
    err = 0 if rho < .75 else min(.6,(rho-.75)*0.9)   # 5xx share
    to = 0 if rho < 1 else min(.25,(rho-1)*.12)        # timeouts
    # k6
    tid = 'demo-stress-spike'
    put('k6_vus',f'testid="{tid}"',t,round(vus_n))
    iters = cnt('iters', thr*STEP/12)
    put('k6_iterations_total',f'testid="{tid}"',t,iters)
    for status,share in (('200',1-err-.02),('404',.01),('401',.01),('503',err*.7),('500',err*.3)):
        put('k6_http_reqs_total',f'testid="{tid}",status="{status}",expected_response="{"true" if status in ("200","401","404") else "false"}"',t,cnt(('k6',status),thr*STEP*max(share,0)))
    base95 = .16*sc; 
    put('k6_http_req_failed_rate',f'testid="{tid}"',t,min(1,err+to))
    put('k6_checks_rate',f'testid="{tid}"',t,max(0,1-err-to*.8-.005))
    for q,mult in (('p50',.55),('p95',1.0),('p99',1.8)):
        v = min(30,base95*mult*(1+to*10))
        put(f'k6_http_req_duration_{q}',f'testid="{tid}"',t,round(v,4))
    for nm,b in (('listings_search',.07),('orders_create',.17),('login',.22),('plots_search',.1)):
        put('k6_http_req_duration_p95',f'testid="{tid}",name="{nm}"',t,round(min(30,b*2.2*sc),4))
    # server side
    tot_share = sum(s for r in SVC.values() for _,_,s,_,_ in r)
    for svc,routes in SVC.items():
        svc_rps=0
        for meth,route,share,bms,e4 in routes:
            rps = thr*share/tot_share*1.0 if thr else 0
            svc_rps += rps
            med = bms/1000*sc
            sigma = .55+.15*min(rho,1.5)
            n_all = rps*STEP
            for code,frac in (('200',1-e4-err),(('400' if meth=='POST' else '404'),e4),('503',err*.7),('500',err*.3)):
                if frac <= 0 and (svc,route,code) not in state: continue
                n = n_all*max(frac,0)*(1+random.gauss(0,.03)) if n_all else 0
                lab = f'service_name="{svc}",http_request_method="{meth}",http_route="{route}",http_response_status_code="{code}"'
                c = cnt(('c',lab),n); state[(svc,route,code)]=1
                put('http_server_request_duration_seconds_count',lab,t,c)
                mean = med*math.exp(sigma**2/2) * (6 if code.startswith('5') else 1)
                put('http_server_request_duration_seconds_sum',lab,t,cnt(('s',lab),n*mean))
                prev=0
                tot=state.get(('c',lab))
                for ub in BOUNDS+[float('inf')]:
                    mu=math.log(med*(8 if code.startswith('5') else 1))
                    p = 1.0 if ub==float('inf') else ncdf((math.log(ub)-mu)/sigma)
                    p = min(p, 1)
                    bc = cnt(('b',lab,ub), n*(p)) if True else 0
                    put('http_server_request_duration_seconds_bucket',lab+f',le="{"+Inf" if ub==float("inf") else ub}"',t,bc)
        cpu = min(1.0, svc_rps/CAP_RPS*2.6)+.02
        put('process_cpu_time_seconds_total',f'service_name="{svc}"',t,cnt(('cpu',svc),(cpu*(1+random.gauss(0,.05)))*STEP))
        put('process_memory_usage_bytes',f'service_name="{svc}"',t,int(160e6+vus*35e3+random.gauss(0,3e6)+ (m*1.5e5 if svc=='order' else 0)))
        put('http_server_active_requests',f'service_name="{svc}"',t,round(max(0,svc_rps*sc*.18*(1+ (rho>1)*4))))
    # VM / cgroup (8 cores, 16 GiB)
    cores=8; cpu_used=min(cores, .4+thr/CAP_RPS*7.2*(1+random.gauss(0,.02)))
    thr_sec = max(0,(cpu_used-6.8))*.35
    put('vm_cgroup_cpu_limit_cores','',t,cores)
    put('vm_cgroup_cpu_usage_seconds_total','',t,cnt('vcpu',cpu_used*STEP))
    put('vm_cgroup_cpu_throttled_seconds_total','',t,cnt('vthr',thr_sec*STEP))
    put('vm_cgroup_memory_limit_bytes','',t,16*2**30)
    anon=3.2e9+vus*2.4e6+random.gauss(0,5e7)
    put('vm_cgroup_memory_anon_bytes','',t,int(anon)); put('vm_cgroup_memory_bytes','',t,int(anon+2.5e9))
    put('node_filesystem_avail_bytes','mountpoint="/"',t,int(41e9-m*2e6)); put('node_filesystem_size_bytes','mountpoint="/"',t,int(80e9))
    # postgres: pool of 100 conns
    active=min(95,round(2+vus_n*0.09*(1+ (rho>.9)*.5))); idle=min(100-active,round(8+vus_n*.02))
    put('pg_up','',t,1); put('pg_settings_max_connections','',t,100)
    put('pg_stat_activity_count','datname="farm",state="active"',t,active)
    put('pg_stat_activity_count','datname="farm",state="idle"',t,idle)
    put('pg_stat_activity_count','datname="farm",state="idle in transaction"',t,round(active*.06))
    put('pg_stat_activity_max_tx_duration','datname="farm"',t,round(.05+sc*.12,3))
    for nm,k in (('xact_commit',1.8),('xact_rollback',.02),('tup_fetched',60),('tup_inserted',.5),('tup_updated',.6),('tup_deleted',.02),('blks_hit',120),('blks_read',1.4+rho*2)):
        put(f'pg_stat_database_{nm}_total'.replace('_total',''),'datname="farm"',t,cnt(('pg',nm),thr*k*STEP))
    put('pg_stat_database_deadlocks','datname="farm"',t,cnt('dl',1 if rho>1.1 and random.random()<.1 else 0))
    put('pg_locks_count','datname="farm",mode="rowexclusivelock"',t,round(active*.8)); 
    # redis
    put('redis_commands_processed_total','',t,cnt('rc',thr*5.5*STEP)); put('redis_memory_used_bytes','',t,int(30e6+vus*9e3)); put('redis_connected_clients','',t,round(12+vus*.03)); put('redis_db_keys','db="db0"',t,round(1500+vus*8))
    # traefik
    for code,share in (('200',1-err-.02),('404',.01),('401',.01),('503',err)):
        put('traefik_entrypoint_requests_total',f'entrypoint="web",code="{code}"',t,cnt(('tr',code),thr*STEP*max(share,0)))
# write
with open(sys.argv[1],'w') as f:
    by={}
    for (name,labels),pts in out.items(): by.setdefault(name,[]).append((labels,pts))
    for name,series in by.items():
        counter = name.endswith('_total') or name.endswith('_count') or name.endswith('_sum') or name.endswith('_bucket') or name.startswith('pg_stat_database_') and 'deadlocks' not in name or name.endswith('deadlocks')
        f.write(f'# TYPE {name} unknown\n')
        for labels,pts in series:
            for ts,v in pts:
                f.write(f'{name}{{{labels}}} {v} {ts}\n' if labels else f'{name} {v} {ts}\n')
    f.write('# EOF\n')
print('range',START,END, 'series',len(out))
