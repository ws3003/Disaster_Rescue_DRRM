# 本轮实际测试命令及边界

全部在 Windows PowerShell 执行。证据目录变量：

```powershell
$evidence = 'D:/《灾害2》/灾害/_handover/阶段核查_20261103_7581d42e'
$env:GIT_OPTIONAL_LOCKS = '0'
```

## A：同一次运行的 99 项与两种报告

工作目录 `D:/《灾害2》/灾害/Disaster_Rescue_DRRM/miniprogram`：

```powershell
node --version
node --test --test-reporter=tap --test-reporter-destination="$evidence/A-tests.tap" --test-reporter=junit --test-reporter-destination="$evidence/A-tests.xml" tests/*.test.js
```

结果：99 通过、0 失败、0 跳过、0 取消；退出码 0。Node v24.16.0。测试完整用例清单在 A-tests.tap 和 A-tests.xml。

## B：当前 S4 / 用户认证 / 媒体用例

工作目录 `D:/《灾害2》/灾害/Disaster_Rescue_DRRM_B`，使用现有 Python 3.11.18 venv，不安装依赖：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TMPDIR = Join-Path $evidence 'temporary'
$env:TEMP = $env:TMPDIR
$env:TMP = $env:TMPDIR
# temporary 为本轮新建目录，不清理或覆盖用户已有目录。
& ./venv/Scripts/python.exe -B -c "import faulthandler,pytest; faulthandler.dump_traceback_later(55,exit=True); raise SystemExit(pytest.main(['tests/test_s4_contract.py','tests/test_auth.py','tests/test_media_access.py','-q','-p','no:cacheprovider','--tb=short','--junitxml=$evidence/B-s4-auth-media-tests.xml']))" *> (Join-Path $evidence 'B-s4-auth-media-tests.log')
```

结果：79 通过、0 失败、0 跳过、37 warnings；退出码 0。按文件：37 + 18 + 24。不能据此确认未取得的 v0.2.0 或文档所称 85 项用例集。

## B：双库两项缺陷复核

同一 B 工作目录、相同环境设置：

```powershell
& ./venv/Scripts/python.exe -B -c "import faulthandler,pytest; faulthandler.dump_traceback_later(45,exit=True); raise SystemExit(pytest.main(['tests/test_c4_s4_flow.py','-q','-p','no:cacheprovider','--tb=short','--junitxml=$evidence/B-c4-s4-tests.xml']))" *> (Join-Path $evidence 'B-c4-s4-tests.log')
```

结果：6 通过、2 失败、0 跳过、1 warning；退出码 1。以下两个用例保持原断言并实际失败：

```text
TestC4S4Flow.test_person_event_reaches_server_s4_after_worker_reports_success
TestC4S4Flow.test_sensor_resolution_reaches_server_after_initial_sync
```

测试使用不同的边缘/服务端 SQLite、真实 bridge/worker/router 和 ASGI，请求记录无鉴权头/Token。它不是 Pi 跨机网络测试；测试数据库及 runtime 目录均不纳入交付 ZIP。

## B：凭据边界与人员生产工具的隔离探测

同一 B 工作目录：

```powershell
& ./venv/Scripts/python.exe -B (Join-Path $evidence 'probe_auth_boundary.py') *> (Join-Path $evidence 'auth-boundary-probe.log')
```

25 个认证边界断言通过，退出码 0。只替换外部微信交换，使用真实登录、Session 查库和三个 router，不启动生产服务、不调用外网。另实际调用 DatabaseTool 验证事件保存及 person_event Outbox pending，不代表完整 Agent 链路通过。

## 执行说明

ASGI 测试在允许线程正常运行的执行环境中进行；受限环境此前存在 TestClient 初始化挂起，本轮设置诊断超时，没有删除用例或降低断言。运行不需要 Git 写操作。完整日志中的资源清理及依赖告警保留，未掩盖为零告警。

## 保留现有工作区的核对

```powershell
# 实际核查脚本只读 Git、源码与文件哈希，输出在证据目录。
& D:/Python/python.exe -B (Join-Path $evidence 'collect_evidence.py')
# 初始核对已在测试前执行；结束时调用：
& D:/Python/python.exe -B (Join-Path $evidence 'collect_evidence.py') --final
```

当前交付中的 before/after 是本轮实际执行结果。不要为“重现”而覆盖这些证据文件，应新建证据目录。
