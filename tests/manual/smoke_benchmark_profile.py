"""Run explicitly on the Linux host: exercises real settings and restoration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

WRAPPER = "/usr/local/bin/agent-reserved-run"
state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
ROOT = Path(os.environ.get("AGENT_BENCHMARK_SMOKE_DIR", state_home / "agent-workspace/benchmark-smoke" / (str(time.time_ns()) + "-" + str(os.getpid()))))
ROOT.mkdir(parents=True, exist_ok=True)


def call(argv, **kw):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def snapshot():
    values = {}
    for p in Path('/sys/devices/system/cpu/cpufreq').glob('policy*'):
        for name in ('scaling_governor', 'energy_performance_preference', 'scaling_min_freq', 'scaling_max_freq', 'boost'):
            if (p/name).exists():
                values[str(p/name)] = (p/name).read_text().strip()
    for p in Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpuidle/state*/disable'):
        values[str(p)] = p.read_text().strip()
    for unit in ('init.scope','system.slice','user.slice','machine.slice'):
        values[unit] = call(['systemctl','show',unit,'-p','AllowedCPUs','--value']).stdout.strip()
    for unit in ('power-profiles-daemon.service','ananicy-cpp.service','snapper-cleanup.timer',
                 'plocate-updatedb.timer','man-db.timer','fstrim.timer','logrotate.timer',
                 'cachyos-rate-mirrors.timer','apt-daily.timer','apt-daily-upgrade.timer'):
        values[unit] = call(['systemctl','is-active',unit]).stdout.strip()
    values['powerdevil'] = call(['systemctl','--user','is-active','plasma-powerdevil.service']).stdout.strip()
    if values['power-profiles-daemon.service'] == 'active':
        profile = call(['powerprofilesctl', 'get'])
        assert profile.returncode == 0, profile.stdout
        values['power_profile'] = profile.stdout.strip()
    values['sched_ext'] = Path('/sys/kernel/sched_ext/state').read_text().strip()
    return values


BASE = snapshot()
RESULTS = []


def restored(name):
    # Include delayed desktop power-policy writes after a service restart.
    time.sleep(1)
    after = snapshot()
    diff = {k: [v, after.get(k)] for k,v in BASE.items() if after.get(k) != v}
    if diff:
        raise AssertionError(f'{name}: restoration differences: {diff}')
    assert not Path('/run/agent-cpu-reserve.json').exists()


def options(scheduler='native', timeout=30, wait=0):
    return [WRAPPER,'--profile','stable','--cpus','16-17','--reserve-cpus','16-19',
            '--scheduler',scheduler,'--timeout',str(timeout),'--wait-lock',str(wait),'--']


VALIDATE = r'''
import json,os,pathlib,subprocess,sys
assert os.getuid()==1000 and os.geteuid()==1000
assert os.sched_getaffinity(0)=={16,17}
assert os.getpriority(os.PRIO_PROCESS,0)==0
cgroup=pathlib.Path('/proc/self/cgroup').read_text().strip()
assert cgroup.startswith('0::/benchmark.slice/agent-reserved-') and cgroup.endswith('.scope')
assert 'NoNewPrivs:\t1' in pathlib.Path('/proc/self/status').read_text()
assert int(pathlib.Path('/proc/self/personality').read_text(),16)&0x40000
assert os.environ['BENCH_TEST']=="space' $\nvalue"
assert sys.argv[1]=="argument with ' spaces and $"
for cpu in [16,17,18,19,112,113,114,115]:
 p=pathlib.Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq')
 assert (p/'scaling_governor').read_text().strip()=='performance'
 assert (p/'energy_performance_preference').read_text().strip()=='performance'
 assert (p/'boost').read_text().strip()=='0'
 assert (p/'scaling_min_freq').read_text().strip()==(p/'scaling_max_freq').read_text().strip()=='2500000'
nested=subprocess.run(['/usr/local/bin/agent-reserved-run','/usr/bin/python3','-c',
 'import os; assert os.getuid()==1000; assert os.sched_getaffinity(0)=={16,17}'],check=True)
print('WORKLOAD_VALIDATED '+json.dumps({'uid':os.getuid(),'cpus':sorted(os.sched_getaffinity(0)),'cgroup':cgroup}))
'''


def single(name, argv, expected=0, env=None):
    start=time.monotonic()
    result=call(argv,timeout=45,env=env)
    (ROOT/f'{name}.log').write_text(result.stdout)
    if expected is None:
        assert result.returncode != 0, result.stdout
    else:
        assert result.returncode==expected, result.stdout
    restored(name)
    RESULTS.append({'name':name,'exit_code':result.returncode,'seconds':time.monotonic()-start,'restored':True})
    print(json.dumps(RESULTS[-1]),flush=True)


env=dict(os.environ, BENCH_TEST="space' $\nvalue")
for scheduler in ('native','bpfland'):
    single(scheduler+'-success',options(scheduler)+['/usr/bin/python3','-c',VALIDATE,"argument with ' spaces and $"],env=env)
single('exit-code',options()+['/usr/bin/python3','-c','raise SystemExit(7)'],expected=7)
single('timeout',options(timeout=3)+['/usr/bin/sleep','20'],expected=None)

SHARED = r'''
import pathlib,subprocess,tempfile,time
wrapper='/usr/local/bin/agent-reserved-run'
with tempfile.TemporaryDirectory() as directory:
 p=pathlib.Path(directory)
 first_code='import pathlib,time,subprocess; p=pathlib.Path('+repr(directory)+'); p.joinpath("ready").touch(); subprocess.run(["/usr/local/bin/agent-reserved-run","/usr/bin/true"],check=True); time.sleep(1.5); p.joinpath("end").write_text(str(time.monotonic()))'
 first=subprocess.Popen([wrapper,'/usr/bin/python3','-c',first_code])
 deadline=time.monotonic()+5
 while not (p/'ready').exists() and time.monotonic()<deadline: time.sleep(.02)
 assert (p/'ready').exists()
 blocked=subprocess.run([wrapper,'/usr/bin/true'])
 assert blocked.returncode==75
 mounts=['/usr/bin/bwrap','--ro-bind','/usr','/usr','--symlink','usr/bin','/bin',
         '--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64',
         '--proc','/proc','--ro-bind','/sys','/sys','--dev','/dev','--ro-bind','/etc','/etc',
         '--dir','/run','--ro-bind','/run/agent-reserved-workload.lock','/run/agent-reserved-workload.lock',
         '--chdir','/','--']
 isolated=subprocess.run(mounts+[wrapper,'/usr/bin/true'])
 assert isolated.returncode==75, 'mount namespace did not share the host workload lock'
 second_code='import pathlib,time; pathlib.Path('+repr(str(p/'second'))+').write_text(str(time.monotonic()))'
 second=subprocess.Popen([wrapper,'--wait-lock','5','/usr/bin/python3','-c',second_code])
 assert first.wait(timeout=8)==0
 assert second.wait(timeout=8)==0
 assert float((p/'second').read_text())>=float((p/'end').read_text())
print('SHARED_SESSION_LOCK_PASSED')
'''
session_options=options()
session_options.insert(-1,'--session')
single('shared-session-lock',session_options+['/usr/bin/python3','-c',SHARED])


def wait_for_state(process):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise AssertionError('benchmark ended before reaching ready state')
        if Path('/run/agent-cpu-reserve.json').exists():
            result=call(['sudo','-n','cat','/run/agent-cpu-reserve.json'])
            if result.returncode==0:
                state=json.loads(result.stdout)
                if call(['systemctl','is-active',state['guard_unit']]).returncode==0:
                    return state
        time.sleep(.1)
    raise AssertionError('profile did not become ready')


for mode in ('terminate','guard-killed','concurrent'):
    with tempfile.TemporaryFile(mode='w+') as output:
        started=time.monotonic()
        process=subprocess.Popen(options('bpfland')+['/usr/bin/sleep','20'],stdout=output,stderr=output,text=True)
        state=wait_for_state(process)
        if mode=='guard-killed':
            result=call(['sudo','-n','systemctl','kill','--kill-whom=main','--signal=KILL',state['guard_unit']])
            assert result.returncode==0,result.stdout
        elif mode=='terminate':
            process.terminate()
        else:
            blocked=call(options(wait=0)+['/usr/bin/true'],timeout=15)
            assert blocked.returncode!=0,blocked.stdout
            assert call(['systemctl','is-active',state['guard_unit']]).returncode==0
            (ROOT/'concurrent-rejected.log').write_text(blocked.stdout)
            expired=call(options(wait=1)+['/usr/bin/true'],timeout=15)
            assert expired.returncode!=0,expired.stdout
            assert call(['systemctl','is-active',state['guard_unit']]).returncode==0
            (ROOT/'concurrent-wait-expired.log').write_text(expired.stdout)
            with tempfile.TemporaryFile(mode='w+') as second_output:
                marker=ROOT/'waiting-command-ran'
                marker.unlink(missing_ok=True)
                waiter=subprocess.Popen(options(wait=10)+['/usr/bin/python3','-c',
                    'import pathlib; pathlib.Path('+repr(str(marker))+').write_text("ran")'],stdout=second_output,stderr=second_output,text=True)
                time.sleep(1)
                assert waiter.poll() is None and not marker.exists(), 'waiting command overlapped the active benchmark'
                process.terminate()
                first_exit=process.wait(timeout=15)
                assert first_exit!=0
                second_exit=waiter.wait(timeout=15)
                second_output.seek(0)
                (ROOT/'concurrent-waiter.log').write_text(second_output.read())
                assert second_exit==0 and marker.read_text()=='ran'
                marker.unlink()
        code=process.wait(timeout=15)
        assert code!=0
        output.seek(0)
        (ROOT/f'{mode}.log').write_text(output.read())
        restored(mode)
        RESULTS.append({'name':mode,'exit_code':code,'seconds':time.monotonic()-started,'restored':True})
        print(json.dumps(RESULTS[-1]),flush=True)

(ROOT/'smoke-results.json').write_text(json.dumps({'checks':RESULTS,'snapshot_entries_checked':len(BASE)},indent=2)+'\n')
print('ALL REAL-HOST CHECKS PASSED',flush=True)
