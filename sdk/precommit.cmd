@echo off
chcp 65001 >nul 2>&1
rem 节点仓一键自检（Windows）：双击可跑，也可被 hook / CI / 计划任务调用。
rem
rem 等价于：
rem   python sdk\check_all.py --with-workflows --with-signature
rem   python sdk\check_signature.py --only-dist
rem
rem 跳过 dist 归档验签：  set SKIP_DIST=1 ^&^& sdk\precommit.cmd
rem 退出码原样透传：0=全绿 1=有未登记失败 2=环境缺失（例如缺平台仓公钥锚，不等于通过）
setlocal

set "ROOT=%~dp0.."
pushd "%ROOT%" >nul 2>nul
if errorlevel 1 (
    echo [precommit] cannot enter repo root: %ROOT%
    exit /b 2
)

set "PY=python"
where %PY% >nul 2>nul
if errorlevel 1 (
    echo [precommit] python not found in PATH ^(or set PYTHON to the interpreter^)
    popd
    exit /b 2
)

echo [precommit] 1/2 packages + workflows meta + official-anchor signature check
echo              %PY% sdk\check_all.py --with-workflows --with-signature
%PY% sdk\check_all.py --with-workflows --with-signature
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto fail

if "%SKIP_DIST%"=="1" (
    echo [precommit] 2/2 skipped ^(SKIP_DIST=1^): dist archive signatures
    goto pass
)

echo [precommit] 2/2 dist archive signatures
echo              %PY% sdk\check_signature.py --only-dist
%PY% sdk\check_signature.py --only-dist
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto fail

:pass
popd
echo [precommit] PASS - all node-repo checks green
exit /b 0

:fail
echo.
echo [precommit] BLOCKED ^(exit %RC%^) - next steps, read:
echo   * package manifest / G1-G10   -^> frameweave-plugins\docs\plugin-development-spec.md
echo   * workflows meta M1-M5        -^> frameweave-plugins\docs\元数据治理报告_workflows_meta_20261026.md
echo   * signature gate / allowlist  -^> frameweave-plugins\docs\验签门报告_20261026.md
echo   * dual-repo workflow gates    -^> docs\双仓分工与开发流程_20261026.md
if "%RC%"=="2" (
    echo   * exit 2 = missing environment ^(usually the platform anchor
    echo     FrameWeave\backend\app\fw_official_pubkey.json, or python not in PATH^) - NOT a pass
)
echo.
echo   emergency bypass ^(after accepting the risk^): git commit --no-verify
popd
exit /b %RC%
