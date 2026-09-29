#!/usr/bin/env node
/**
 * Blueprint Automation 一键启动引导脚本
 *
 * 分工（重要）：
 *   start.cmd    —— 纯 ASCII 启动器，只检查 node 后转发到本文件，不含任何真实逻辑。
 *   本文件        —— 环境依赖编排（node 版本 / python 探测 / 创建 .venv / 装依赖）+ 中文菜单 + 调度。
 *   scripts/bp_dev.py —— 真正的构建与启动逻辑（run / build-editor / build-runtime / env）。
 *
 * 用法：
 *   start.cmd                      交互式中文菜单
 *   start.cmd dev                  直接启动编辑器
 *   start.cmd deps                 只检查/安装依赖
 *   start.cmd build                打包发布版（编辑器 + 运行时模板）
 *   start.cmd build-runtime        只重打运行时模板
 *   start.cmd env                  查看环境信息与模板新鲜度
 *   追加 --force 强制重装依赖；追加 --yes 跳过交互（等价菜单默认项 1）
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline/promises';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const IS_WIN = process.platform === 'win32';

const VENV_DIR = path.join(ROOT, '.venv');
const VENV_PY = IS_WIN
  ? path.join(VENV_DIR, 'Scripts', 'python.exe')
  : path.join(VENV_DIR, 'bin', 'python');
const DEV_PY = path.join(ROOT, 'scripts', 'bp_dev.py');
const REQUIREMENTS = path.join(ROOT, 'requirements.txt');

const NEED_NODE_MAJOR = 18; // 本项目引导脚本需要 Node 18+
const NEED_PY = [3, 10];    // 编辑器需要 Python 3.10+

const c = {
  reset: '\x1b[0m', bold: '\x1b[1m', dim: '\x1b[2m',
  green: '\x1b[32m', yellow: '\x1b[33m', red: '\x1b[31m', cyan: '\x1b[36m',
};
const log = (m = '') => console.log(m);
const ok = (m) => log(`${c.green}[OK]${c.reset} ${m}`);
const warn = (m) => log(`${c.yellow}[警告]${c.reset} ${m}`);
const fail = (m) => { console.error(`${c.red}[错误]${c.reset} ${m}`); process.exitCode = 1; };

// ---------- 进程工具（不用 shell：命令与参数逐项传递，避免引号/转义坑） ----------

function runSync(cmd, args, opts = {}) {
  return spawnSync(cmd, args, { cwd: ROOT, encoding: 'utf8', ...opts });
}

function runStreamed(cmd, args, opts = {}) {
  const child = spawn(cmd, args, { cwd: ROOT, stdio: 'inherit', ...opts });
  return new Promise((resolve) => {
    child.on('close', (code) => resolve(code ?? 1));
    child.on('error', () => resolve(1));
  });
}

// ---------- 环境检查 ----------

function checkNode() {
  const res = runSync('node', ['--version']);
  const m = (res.stdout || '').trim().match(/^v(\d+)\./);
  const major = m ? Number(m[1]) : 0;
  if (major < NEED_NODE_MAJOR) {
    fail(`Node.js 版本过低（当前 ${(res.stdout || '').trim() || '未知'}），引导脚本需要 ${NEED_NODE_MAJOR}+。`);
    log('  请到 https://nodejs.org/ 安装或升级后重试。');
    return false;
  }
  ok(`Node.js ${(res.stdout || '').trim()}（要求 >= ${NEED_NODE_MAJOR}）`);
  return true;
}

/** 探测一个候选解释器的 major.minor */
function probePython(cmd, preArgs = []) {
  const code = 'import sys;print("%d.%d" % sys.version_info[:2])';
  const res = runSync(cmd, [...preArgs, '-c', code]);
  if (res.status !== 0) return null;
  const m = (res.stdout || '').trim().match(/(\d+)\.(\d+)/);
  if (!m) return null;
  return { cmd, args: preArgs, major: Number(m[1]), minor: Number(m[2]) };
}

function versionOk(v) {
  return v.major > NEED_PY[0] || (v.major === NEED_PY[0] && v.minor >= NEED_PY[1]);
}

/** 找一个系统 Python（用于创建 venv） */
function findSystemPython() {
  const candidates = IS_WIN
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]];
  const found = [];
  for (const [cmd, pre] of candidates) {
    const v = probePython(cmd, pre);
    if (v) found.push(v);
  }
  if (!found.length) return null;
  const good = found.find(versionOk);
  return good || found[0];
}

/** 确保 .venv 存在，返回可用的解释器路径 */
async function ensureVenv() {
  if (fs.existsSync(VENV_PY)) {
    const v = probePython(VENV_PY);
    if (v && versionOk(v)) {
      ok(`虚拟环境已就绪：.venv（Python ${v.major}.${v.minor}）`);
      return VENV_PY;
    }
    warn('.venv 存在但解释器不可用或版本过低，将重建。');
  }

  const sysPy = findSystemPython();
  if (!sysPy) {
    fail('未找到 Python。本项目需要 Python 3.10+。');
    log('  安装方式：https://www.python.org/downloads/ （安装时勾选 “Add Python to PATH”）');
    log('  装好后重开终端再运行 start.cmd。');
    return null;
  }
  if (!versionOk(sysPy)) {
    fail(`系统 Python 版本过低（当前 ${sysPy.major}.${sysPy.minor}），需要 ${NEED_PY.join('.')}+。`);
    log('  请升级 Python 后重试：https://www.python.org/downloads/');
    return null;
  }

  log(`正在创建虚拟环境 .venv（使用 ${sysPy.cmd}，Python ${sysPy.major}.${sysPy.minor}）……`);
  const code = await runStreamed(sysPy.cmd, [...sysPy.args, '-m', 'venv', '.venv']);
  if (code !== 0 || !fs.existsSync(VENV_PY)) {
    fail('创建虚拟环境失败。可手动执行：');
    log(`  ${sysPy.cmd} ${sysPy.args.join(' ')} -m venv .venv`.trim());
    return null;
  }
  ok('虚拟环境创建完成：.venv');
  return VENV_PY;
}

const KEY_IMPORTS = ['PySide6', 'cv2', 'numpy', 'PIL', 'win32gui', 'psutil', 'pynput'];

/** 检查关键依赖；缺失（或 force）时按 requirements.txt 安装 */
async function ensureDeps(py, { force = false } = {}) {
  if (!force) {
    const code = KEY_IMPORTS.map((m) => `import ${m}`).join('; ');
    const res = runSync(py, ['-c', code]);
    if (res.status === 0) {
      ok('关键依赖已就绪（PySide6 / cv2 / numpy / PIL / win32gui / psutil / pynput）');
      return true;
    }
    warn('部分关键依赖缺失，准备安装。');
  }
  if (!fs.existsSync(REQUIREMENTS)) {
    fail(`找不到依赖清单：${REQUIREMENTS}`);
    return false;
  }
  log(force ? '强制重新安装依赖（requirements.txt）……' : '正在安装依赖（requirements.txt）……');
  // 刻意不升级 pip：受限环境下 pip 自卸载会触发安全删除失败，导致 venv 的 pip 损坏。
  const code = await runStreamed(py, ['-m', 'pip', 'install', '-r', REQUIREMENTS]);
  if (code !== 0) {
    warn('pip 退出码非 0（个别包如 PyAudio 可能需要编译环境）。');
    warn(`  可稍后手动重试：${VENV_PY} -m pip install -r requirements.txt`);
    warn('  PyAudio 编译报错的解决办法见：assets/doc/44_faq_env.md');
    return false;
  }
  ok('依赖安装完成');
  return true;
}

/** 转发到 Python 侧真实实现 */
async function dispatch(py, sub, extraArgs = []) {
  if (!fs.existsSync(DEV_PY)) {
    fail(`找不到 Python 侧脚本：${DEV_PY}`);
    return 1;
  }
  return runStreamed(py, [DEV_PY, sub, ...extraArgs]);
}

// ---------- 菜单 ----------

function printBanner() {
  log('');
  log(`${c.bold}${c.cyan}==============================================${c.reset}`);
  log(`${c.bold}${c.cyan}  Blueprint Automation 一键启动器${c.reset}`);
  log(`${c.bold}${c.cyan}==============================================${c.reset}`);
  log(`${c.dim}  启动器：start.cmd → start.mjs → scripts/bp_dev.py${c.reset}`);
  log('');
}

function printMenu() {
  log('请选择操作：');
  log(`  ${c.bold}1${c.reset}. 启动编辑器（开发模式，自动检查依赖）`);
  log(`  ${c.bold}2${c.reset}. 仅检查 / 安装环境依赖（不启动）`);
  log(`  ${c.bold}3${c.reset}. 打包发布版（编辑器 + 运行时模板 → dist\\BlueprintAutomation）`);
  log(`  ${c.bold}4${c.reset}. 仅重打运行时模板（供「导出为独立 EXE」使用）`);
  log(`  ${c.bold}5${c.reset}. 查看环境信息（版本 / 依赖 / 模板新鲜度）`);
  log(`  ${c.bold}6${c.reset}. 退出`);
  log('');
}

const SUBCOMMANDS = {
  dev: 'run',
  run: 'run',
  deps: '__deps_only__',
  build: 'build-all',
  'build-all': 'build-all',
  'build-editor': 'build-editor',
  'build-runtime': 'build-runtime',
  env: 'env',
};

async function main() {
  const argv = process.argv.slice(2);
  const force = argv.includes('--force');
  const nonInteractive = argv.includes('--yes') || argv.includes('-y');
  const subArg = argv.find((a) => !a.startsWith('-'));
  // 其余开关（如 --dry-run、--no-install）原样透传给 Python 侧
  const passthrough = argv.filter((a) => a.startsWith('-') && !['--force', '--yes', '-y'].includes(a));

  printBanner();

  if (!checkNode()) return;

  const py = await ensureVenv();
  if (!py) return;

  let choice;
  if (subArg) {
    if (!(subArg in SUBCOMMANDS)) {
      fail(`未知命令：${subArg}`);
      log('  可用命令：dev | deps | build | build-editor | build-runtime | env');
      return;
    }
    choice = SUBCOMMANDS[subArg];
    if (choice === '__deps_only__') {
      await ensureDeps(py, { force });
      ok('依赖处理完成，未启动任何程序。');
      return;
    }
    log(`${c.dim}（命令模式：${subArg}）${c.reset}`);
  } else if (nonInteractive) {
    choice = '1';
  } else {
    printMenu();
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      const answer = (await rl.question('输入选项 [1-6]（默认 1）：')).trim();
      choice = answer || '1';
    } finally {
      rl.close();
    }
    log('');
  }

  switch (choice) {
    case '1':
    case 'run': {
      if (!(await ensureDeps(py, { force }))) {
        warn('依赖未完全就绪，仍尝试启动编辑器（缺包时会报导入错误）。');
      }
      const extra = [...(force ? ['--force'] : []), ...passthrough];
      const code = await dispatch(py, 'run', extra);
      if (code === 0) ok('编辑器已退出。');
      else fail(`编辑器退出码 ${code}`);
      return;
    }
    case '__deps_only__':
    case '2': {
      await ensureDeps(py, { force });
      ok('依赖处理完成，未启动。之后运行 start.cmd 选 1 即可启动编辑器。');
      return;
    }
    case 'build-all':
    case '3': {
      await ensureDeps(py, { force });
      const code = await dispatch(py, 'build-all', passthrough);
      if (code === 0) ok('打包完成。');
      else fail(`打包未通过（退出码 ${code}），详见上方日志。`);
      return;
    }
    case 'build-runtime':
    case '4': {
      const code = await dispatch(py, 'build-runtime', passthrough);
      if (code === 0) ok('运行时模板已更新，可以导出 EXE 了。');
      else fail(`运行时模板构建失败（退出码 ${code}）。`);
      return;
    }
    case 'build-editor': {
      await dispatch(py, 'build-editor', passthrough);
      return;
    }
    case 'env':
    case '5': {
      await dispatch(py, 'env', passthrough);
      return;
    }
    case '6':
      log('已退出。');
      return;
    default:
      warn(`无效选项：${choice}，已退出。`);
      return;
  }
}

main().catch((err) => {
  fail(`引导脚本异常：${err?.message || err}`);
  process.exit(1);
});
