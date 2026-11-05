"use strict";

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const HOST = "127.0.0.1";
// 8787 em produção; KEYFORGE_PORT permite testar sem conflitar com uma instância aberta.
const PORT = Number(process.env.KEYFORGE_PORT || 8787);
const ORIGIN = `http://${HOST}:${PORT}`;
const START_TIMEOUT_MS = 45000;
const HEALTH_INTERVAL_MS = 350;

let mainWindow = null;
let backend = null;
let backendOwned = false;
let quitting = false;
let ready = false;
let backendExit = null;
let logFile = "";

app.setAppUserModelId("com.keyforge.app");

function logPath() {
  if (!logFile) {
    const dir = path.join(app.getPath("appData"), "KeyForge", "logs");
    logFile = path.join(dir, "electron-backend.log");
  }
  return logFile;
}

function backendExecutable() {
  if (process.env.KEYFORGE_BACKEND_EXE) {
    return path.resolve(process.env.KEYFORGE_BACKEND_EXE);
  }
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "backend", "keyforge-backend.exe");
  }
  return path.join(__dirname, "..", "dist", "keyforge-backend.exe");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function healthJson() {
  return new Promise((resolve, reject) => {
    const request = http.get(`${ORIGIN}/api/health`, { timeout: 1200 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
        if (body.length > 65536) request.destroy(new Error("resposta muito grande"));
      });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          reject(new Error(`HTTP ${response.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on("timeout", () => request.destroy(new Error("timeout")));
    request.on("error", reject);
  });
}

async function keyForgeIsRunning() {
  try {
    const data = await healthJson();
    return data && data.ok === true && data.app === "KeyForge";
  } catch {
    return false;
  }
}

function startBackend() {
  const executable = backendExecutable();
  if (!fs.existsSync(executable)) {
    throw new Error(
      `Backend não encontrado em:\n${executable}\n\n` +
        "Rode `npm run build` antes de abrir o app em desenvolvimento."
    );
  }

  fs.mkdirSync(path.dirname(logPath()), { recursive: true });
  const log = fs.createWriteStream(logPath(), { flags: "a" });
  log.write(`\n[${new Date().toISOString()}] ${executable} --port ${PORT} --no-browser\n`);

  backendExit = null;
  backend = spawn(executable, ["--port", String(PORT), "--host", HOST, "--no-browser"], {
    cwd: path.dirname(executable),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backendOwned = true;

  backend.stdout.on("data", (chunk) => {
    log.write(chunk);
    process.stdout.write(chunk);
  });
  backend.stderr.on("data", (chunk) => {
    log.write(chunk);
    process.stderr.write(chunk);
  });
  backend.on("error", (error) => {
    log.write(`falha ao iniciar backend: ${error.message}\n`);
  });
  backend.once("exit", (code, signal) => {
    log.write(`backend encerrou (code=${code} signal=${signal})\n`);
    backendExit = { code, signal };
    backend = null;
    backendOwned = false;
    if (!quitting && mainWindow) {
      dialog.showErrorBox(
        "KeyForge",
        `O backend encerrou inesperadamente (código ${code ?? signal ?? "?"}).\n\n` +
          `Log: ${logPath()}`
      );
      app.quit();
    }
  });
}

async function waitForBackend() {
  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await keyForgeIsRunning()) return;
    if (backendExit) {
      throw new Error(
        `O backend encerrou durante a inicialização (código ${backendExit.code ?? "?"}). ` +
          "Verifique se a porta 8787 está livre.\n" +
          `Log: ${logPath()}`
      );
    }
    await sleep(HEALTH_INTERVAL_MS);
  }
  throw new Error(
    "Tempo esgotado aguardando o backend em 127.0.0.1:8787.\n" + `Log: ${logPath()}`
  );
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1380,
    height: 880,
    minWidth: 980,
    minHeight: 640,
    show: false,
    backgroundColor: "#0b0d10",
    title: "KeyForge",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  mainWindow.once("ready-to-show", () => {
    ready = true;
    mainWindow.show();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url).catch(() => {});
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(ORIGIN)) {
      event.preventDefault();
      if (/^https?:\/\//i.test(url)) shell.openExternal(url).catch(() => {});
    }
  });

  mainWindow.loadURL(ORIGIN);
}

function stopBackend() {
  if (!backend || !backendOwned) return;
  const child = backend;
  backend = null;
  backendOwned = false;
  try {
    if (process.platform === "win32") {
      // O onefile do PyInstaller cria um processo filho: /T encerra a árvore.
      spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
    } else {
      child.kill("SIGTERM");
    }
  } catch {
    // O processo já pode ter saído sozinho.
  }
}

async function bootstrap() {
  Menu.setApplicationMenu(null);
  if (!(await keyForgeIsRunning())) {
    startBackend();
    await waitForBackend();
  }
  createWindow();
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(bootstrap).catch((error) => {
    dialog.showErrorBox("KeyForge não pôde iniciar", String(error.message || error));
    app.quit();
  });

  app.on("before-quit", () => {
    quitting = true;
    stopBackend();
  });
  app.on("window-all-closed", () => app.quit());
  app.on("activate", () => {
    if (!mainWindow && ready) createWindow();
  });
  process.on("exit", stopBackend);
}
