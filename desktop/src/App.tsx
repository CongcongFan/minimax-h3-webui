import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from "@xyflow/react";
import { open } from "@tauri-apps/plugin-dialog";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { isPermissionGranted, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import {
  AlertTriangle,
  ChevronDown,
  CircleDollarSign,
  Cloud,
  FolderOpen,
  Gauge,
  HardDrive,
  Images,
  LoaderCircle,
  Palette,
  Play,
  Plus,
  Save,
  Settings2,
  Square,
  Trash2,
  Upload,
  Video,
  X,
} from "lucide-react";
import { H3_NODE_TYPES } from "./components/H3Nodes";
import {
  createProject,
  importProjectAssets,
  isTauriRuntime,
  loadProject,
  loadJobs,
  newProject,
  saveProject,
  updateNodeData,
  writeJob,
} from "./lib/project";
import { clearPendingJobs, compileJobs, estimatedCost } from "./lib/queue";
import { PodExecutor, type GatewayHealth } from "./lib/executors";
import {
  QUALITY_PRESETS,
  type ExecutorQuote,
  type ExecutorSession,
  type H3CanvasNode,
  type H3Project,
  type JobSnapshot,
  type ProjectAsset,
  type QualityPresetId,
} from "./types";

const executor = new PodExecutor();

type UiTheme = {
  id: string;
  name: string;
  base: string;
  accent: string;
};

const THEME_PRESETS: UiTheme[] = [
  { id: "forest", name: "森林", base: "#0c1512", accent: "#79b98f" },
  { id: "graphite", name: "石墨", base: "#111317", accent: "#91a7ff" },
  { id: "midnight", name: "深海", base: "#0a1020", accent: "#62a8ff" },
  { id: "plum", name: "暮紫", base: "#160f19", accent: "#c58bd5" },
  { id: "cocoa", name: "暖棕", base: "#19130f", accent: "#d5a260" },
];

function initialTheme(): UiTheme {
  try {
    const saved = localStorage.getItem("h3-ui-theme");
    if (saved) return { ...THEME_PRESETS[0], ...JSON.parse(saved) };
  } catch {
    // 损坏的本地偏好不会影响应用启动。
  }
  return THEME_PRESETS[0];
}

function elapsedLabel(seconds: number) {
  const minutes = Math.round(seconds / 60);
  return minutes < 1 ? "少于 1 分钟" : `约 ${minutes} 分钟`;
}

function statusLabel(status: JobSnapshot["status"]) {
  const labels: Record<JobSnapshot["status"], string> = {
    queued: "等待中",
    paused: "已暂停",
    starting: "启动 GPU",
    uploading: "上传素材",
    running: "生成中",
    enhancing: "高清增强",
    downloading: "回收成片",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status];
}

function startupProgressLabel(health: GatewayHealth) {
  const downloaded = health.downloadedBytes ?? 0;
  const total = health.totalBytes ?? 0;
  const amount = total > 0
    ? ` · ${(downloaded / 1024 ** 3).toFixed(1)} / ${(total / 1024 ** 3).toFixed(1)} GB`
    : "";
  const file = health.currentFile ? ` · ${health.currentFile}` : "";
  return `${health.stage || "启动云端执行器"}${amount}${file}`;
}

export default function App() {
  const [project, setProject] = useState<H3Project>(() => newProject("板栗 · 停车场"));
  const [projectPath, setProjectPath] = useState(() =>
    isTauriRuntime() ? "" : "browser://welcome/project.h3.json",
  );
  const [jobs, setJobs] = useState<JobSnapshot[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState("generate");
  const [quote, setQuote] = useState<ExecutorQuote | null>(null);
  const [session, setSession] = useState<ExecutorSession | null>(null);
  const [notice, setNotice] = useState("项目尚未保存到 iCloud");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [theme, setTheme] = useState<UiTheme>(initialTheme);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [pendingImportAfterCreate, setPendingImportAfterCreate] = useState(false);
  const [gpuConfirmOpen, setGpuConfirmOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [runpodConnected, setRunpodConnected] = useState(false);
  const [connectionReport, setConnectionReport] = useState("");
  const [busy, setBusy] = useState(false);
  const [idleLeft, setIdleLeft] = useState<number | null>(null);
  const [draggingAssets, setDraggingAssets] = useState(false);
  const saveTimer = useRef<number | undefined>(undefined);
  const runnerBusy = useRef(false);
  const liveSessionId = useRef<string | null>(null);
  const pendingDroppedPaths = useRef<string[]>([]);

  async function notify(title: string, body: string) {
    if (!project.settings.notificationsEnabled || !isTauriRuntime()) return;
    if (await isPermissionGranted()) sendNotification({ title, body });
  }

  async function setNotifications(enabled: boolean) {
    if (!enabled) {
      setProject((current) => ({ ...current, settings: { ...current.settings, notificationsEnabled: false } }));
      return;
    }
    const permission = await requestPermission();
    const granted = permission === "granted";
    setProject((current) => ({ ...current, settings: { ...current.settings, notificationsEnabled: granted } }));
    setNotice(granted ? "系统通知已启用" : "系统没有授予通知权限");
  }

  const selectedNode = project.nodes.find((node) => node.id === selectedNodeId);
  const activeJob = jobs.find((job) =>
    ["starting", "uploading", "running", "enhancing", "downloading"].includes(job.status),
  );

  async function restoreJobsAndSession(path: string, loaded: H3Project) {
    const restoredJobs = await loadJobs(path);
    const interrupted = restoredJobs.find((job) =>
      ["starting", "uploading", "running", "enhancing", "downloading"].includes(job.status),
    );
    if (interrupted?.remote?.sessionId) {
      try {
        const recovered = await invoke<ExecutorSession>("runpod_recover_session", {
          podId: interrupted.remote.sessionId,
          gpu: loaded.settings.preferredGpu,
          proxyPort: loaded.settings.runpodProxyPort,
        });
        setSession(recovered);
        setNotice("已找到上次 GPU 会话，正在恢复任务状态");
      } catch (error) {
        const failed = restoredJobs.map((job) =>
          ["starting", "uploading", "running", "enhancing", "downloading"].includes(job.status)
            ? { ...job, status: "failed" as const, error: `恢复失败：${String(error)}` }
            : job,
        );
        setJobs(failed);
        await Promise.all(failed.map((job) => writeJob(path, job)));
        return;
      }
    }
    setJobs(restoredJobs);
  }

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const recent = localStorage.getItem("h3-recent-project");
    if (!recent) return;
    loadProject(recent)
      .then(async (loaded) => {
        setProject(loaded);
        setProjectPath(recent);
        await restoreJobsAndSession(recent, loaded);
        setNotice((current) => current.includes("GPU 会话") ? current : "已恢复上次项目");
      })
      .catch(() => localStorage.removeItem("h3-recent-project"));
  }, []);

  const updateNodes = useCallback((changes: NodeChange<H3CanvasNode>[]) => {
    setProject((current) => ({
      ...current,
      nodes: applyNodeChanges(changes, current.nodes),
    }));
  }, []);

  const updateEdges = useCallback((changes: EdgeChange[]) => {
    setProject((current) => ({
      ...current,
      edges: applyEdgeChanges(changes, current.edges),
    }));
  }, []);

  const connect = useCallback((connection: Connection) => {
    setProject((current) => ({ ...current, edges: addEdge(connection, current.edges) }));
  }, []);

  const patchNode = useCallback((nodeId: string, patch: Record<string, unknown>) => {
    setProject((current) => ({
      ...current,
      nodes: updateNodeData(current.nodes, nodeId, patch),
    }));
  }, []);

  useEffect(() => {
    window.clearTimeout(saveTimer.current);
    if (!projectPath) return;
    saveTimer.current = window.setTimeout(() => {
      saveProject(projectPath, project)
        .then(() => setNotice(isTauriRuntime() ? "已自动保存到项目" : "浏览器预览已保存"))
        .catch((error) => setNotice(`保存失败：${String(error)}`));
    }, 900);
    return () => window.clearTimeout(saveTimer.current);
  }, [project, projectPath]);

  useEffect(() => {
    const queued = jobs[0];
    if (!queued) return;
    executor.quote(queued, project.settings).then(setQuote).catch(() => undefined);
  }, [jobs, project.settings.preferredGpu]);

  useEffect(() => {
    if (!session || activeJob || jobs.some((job) => job.status === "queued")) {
      setIdleLeft(null);
      return;
    }
    setIdleLeft(project.settings.idleTerminateMinutes * 60);
    const timer = window.setInterval(() => {
      setIdleLeft((left) => {
        if (left == null) return left;
        if (left <= 1) {
          executor.terminate(session)
            .then(() => notify("H3 GPU 已自动关闭", "队列空闲达到设定时间，RunPod Pod 已终止。"))
            .catch(() => undefined);
          setSession(null);
          return null;
        }
        return left - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [session?.id, Boolean(activeJob), jobs.filter((job) => job.status === "queued").length]);

  useEffect(() => {
    liveSessionId.current = session?.id ?? null;
  }, [session?.id]);

  useEffect(() => {
    try {
      localStorage.setItem("h3-ui-theme", JSON.stringify(theme));
    } catch {
      // 外观偏好保存失败时仍保持当前会话可用。
    }
  }, [theme]);

  useEffect(() => {
    if (!settingsOpen || !isTauriRuntime()) return;
    invoke<boolean>("has_secret", { account: "runpod-api-key" })
      .then(setRunpodConnected)
      .catch(() => setRunpodConnected(false));
  }, [settingsOpen]);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | undefined;
    getCurrentWindow().onDragDropEvent(async (event) => {
      if (event.payload.type === "enter" || event.payload.type === "over") {
        setDraggingAssets(true);
      } else if (event.payload.type === "leave") {
        setDraggingAssets(false);
      } else if (event.payload.type === "drop") {
        setDraggingAssets(false);
        try {
          if (!projectPath) {
            pendingDroppedPaths.current = [...event.payload.paths];
            setPendingImportAfterCreate(true);
            setNewProjectName("");
            setNewProjectOpen(true);
            setNotice(`已记住 ${event.payload.paths.length} 个素材，创建项目后会自动导入`);
            return;
          }
          await importPathsIntoProject(projectPath, event.payload.paths);
        } catch (error) {
          setNotice(`导入失败：${String(error)}`);
        }
      }
    }).then((dispose) => { unlisten = dispose; });
    return () => unlisten?.();
  }, [projectPath]);

  useEffect(() => {
    const next = jobs.find((job) => job.status === "queued");
    if (!session || !next || runnerBusy.current || !isTauriRuntime()) return;
    runnerBusy.current = true;

    const persist = async (updated: JobSnapshot) => {
      setJobs((current) => current.map((job) => job.id === updated.id ? updated : job));
      await writeJob(projectPath, updated);
    };

    const run = async () => {
      let current: JobSnapshot = {
        ...next,
        status: "starting",
        updatedAt: new Date().toISOString(),
        timing: { ...next.timing, taskStartedAt: new Date().toISOString() },
      };
      try {
        await persist(current);
        setNotice("等待云端环境和 H3 节点准备完成…");
        const ready = session.status === "ready" ? session : await executor.waitUntilReady(
          session,
          (health) => setNotice(startupProgressLabel(health)),
        );
        if (liveSessionId.current !== ready.id) throw new Error("GPU 会话已被终止");
        setSession(ready);

        const sessionElapsedSeconds = Math.max(
          0,
          (Date.now() - new Date(ready.startedAt).getTime()) / 1000,
        );
        const rate = ready.hourlyRateUsd || quote?.hourlyRateUsd || 0;
        if (
          project.settings.sessionSoftBudgetUsd > 0 &&
          estimatedCost(rate, sessionElapsedSeconds) >= project.settings.sessionSoftBudgetUsd
        ) {
          current = { ...current, status: "paused", updatedAt: new Date().toISOString() };
          await persist(current);
          setJobs((all) => all.map((job) => job.status === "queued" ? { ...job, status: "paused" } : job));
          setNotice("会话已达到费用软提醒；任务已暂停，正在运行的内容没有被中断");
          return;
        }

        current = {
          ...current,
          status: "uploading",
          updatedAt: new Date().toISOString(),
          timing: { ...current.timing, sessionStartedAt: ready.startedAt, uploadStartedAt: new Date().toISOString() },
          remote: { executor: "pod", sessionId: ready.id },
        };
        await persist(current);
        setNotice(`正在上传 ${current.assetPaths.length} 个本次任务所需素材…`);
        const remotePaths = await executor.uploadProjectAssets(
          ready,
          projectPath,
          current.assetPaths,
        );
        const submittedPayload = { ...current, assetPaths: remotePaths };
        const remoteJobId = await executor.submit(ready, submittedPayload);
        current = {
          ...current,
          status: "running",
          updatedAt: new Date().toISOString(),
          remote: { executor: "pod", sessionId: ready.id, remoteJobId },
          timing: { ...current.timing, generationStartedAt: new Date().toISOString() },
        };
        await persist(current);

        const deadline = Date.now() + project.settings.jobTimeoutMinutes * 60_000;
        while (Date.now() < deadline) {
          if (liveSessionId.current !== ready.id) throw new Error("GPU 会话已被终止");
          const remote = await executor.status(ready, remoteJobId);
          if (remote.stage?.includes("SeedVR2") && current.status !== "enhancing") {
            current = {
              ...current,
              status: "enhancing",
              updatedAt: new Date().toISOString(),
              timing: { ...current.timing, enhancementStartedAt: new Date().toISOString() },
            };
            await persist(current);
          }
          setNotice(`${current.label} · ${remote.stage ?? "生成中"}${remote.progress != null ? ` · ${Math.round(remote.progress * 100)}%` : ""}`);
          if (remote.status === "failed" || remote.status === "cancelled") {
            throw new Error(remote.error || "云端任务失败");
          }
          if (remote.status === "succeeded") break;
          await new Promise((resolve) => window.setTimeout(resolve, 3500));
        }
        if (Date.now() >= deadline) {
          await executor.cancel(ready, remoteJobId);
          throw new Error(`任务超过 ${project.settings.jobTimeoutMinutes} 分钟，已请求取消`);
        }

        current = { ...current, status: "downloading", updatedAt: new Date().toISOString(), timing: { ...current.timing, downloadStartedAt: new Date().toISOString() } };
        await persist(current);
        setNotice("生成完成，正在校验并回收到 Mac…");
        const output = await executor.downloadArtifacts(ready, remoteJobId, projectPath);
        const finishedAt = new Date().toISOString();
        const secondsBetween = (from?: string, to?: string) => from && to ? Math.max(0, (new Date(to).getTime() - new Date(from).getTime()) / 1000) : 0;
        const timing = { ...current.timing, finishedAt };
        const generationEnd = timing.enhancementStartedAt ?? timing.downloadStartedAt;
        const phaseSeconds = {
          startup: secondsBetween(timing.taskStartedAt, timing.uploadStartedAt),
          upload: secondsBetween(timing.uploadStartedAt, timing.generationStartedAt),
          generation: secondsBetween(timing.generationStartedAt, generationEnd),
          enhancement: secondsBetween(timing.enhancementStartedAt, timing.downloadStartedAt),
          download: secondsBetween(timing.downloadStartedAt, timing.finishedAt),
        };
        const breakdown = Object.fromEntries(Object.entries(phaseSeconds).map(([phase, seconds]) => [phase, { seconds, estimatedUsd: estimatedCost(rate, seconds) }])) as NonNullable<JobSnapshot["cost"]>["breakdown"];
        const totalSeconds = Object.values(phaseSeconds).reduce((sum, seconds) => sum + seconds, 0);
        current = {
          ...current,
          status: "succeeded",
          updatedAt: finishedAt,
          outputRelativePath: output,
          timing,
          cost: {
            hourlyRateUsd: rate,
            estimatedUsd: estimatedCost(rate, totalSeconds),
            breakdown,
          },
        };
        await persist(current);
        setNotice(`${current.label} 已完成并通过媒体校验`);
        await notify("H3 成片已完成", `${current.label} 已下载到项目 Outputs，并通过媒体校验。`);
      } catch (error) {
        if (current.status === "starting" && session) {
          let diagnostic: Record<string, unknown> = {
            capturedAt: new Date().toISOString(),
            sessionId: session.id,
            error: String(error),
          };
          try {
            diagnostic = { ...diagnostic, gateway: await executor.diagnostics(session) };
          } catch (diagnosticError) {
            diagnostic.diagnosticError = String(diagnosticError);
          }
          await invoke("write_job_diagnostic", {
            projectPath,
            jobId: current.id,
            diagnostic,
          }).catch(() => undefined);
          await executor.terminate(session).catch(() => undefined);
          setSession(null);
        }
        if (current.status !== "paused") {
          current = {
            ...current,
            status: "failed",
            updatedAt: new Date().toISOString(),
            error: String(error),
            timing: { ...current.timing, finishedAt: new Date().toISOString() },
          };
          await persist(current).catch(() => undefined);
          setNotice(`任务失败：${String(error)}`);
          await notify("H3 任务失败", `${current.label}：${String(error).slice(0, 180)}`);
        }
      } finally {
        runnerBusy.current = false;
      }
    };

    void run();
  }, [session?.id, jobs.find((job) => job.status === "queued")?.id, projectPath]);

  async function createNewProject() {
    setNewProjectName("");
    setNewProjectOpen(true);
  }

  function closeNewProjectModal() {
    pendingDroppedPaths.current = [];
    setPendingImportAfterCreate(false);
    setNewProjectOpen(false);
  }

  async function confirmCreateProject() {
    try {
      const name = newProjectName.trim();
      if (!name) return;
      if (!isTauriRuntime()) {
        const created = await createProject("browser", name);
        setProject(created.project);
        setProjectPath(created.projectPath);
        setJobs([]);
        setNewProjectOpen(false);
        const shouldContinueImport = pendingImportAfterCreate;
        setPendingImportAfterCreate(false);
        setNotice(shouldContinueImport ? "项目已创建，请选择要导入的素材" : "浏览器预览已保存");
        if (shouldContinueImport) {
          window.setTimeout(() => document.getElementById("browser-assets")?.click(), 0);
        }
        return;
      }
      const destination = await invoke<string>("default_project_parent");
      const created = await createProject(destination, name);
      const droppedPaths = pendingDroppedPaths.current;
      const shouldContinueImport = pendingImportAfterCreate;
      pendingDroppedPaths.current = [];
      setPendingImportAfterCreate(false);
      setProject(created.project);
      setProjectPath(created.projectPath);
      localStorage.setItem("h3-recent-project", created.projectPath);
      setJobs([]);
      setNewProjectOpen(false);
      setNotice("项目已创建并保存");
      try {
        if (droppedPaths.length > 0) {
          await importPathsIntoProject(created.projectPath, droppedPaths);
        } else if (shouldContinueImport) {
          await chooseAndImportAssets(created.projectPath);
        }
      } catch (error) {
        setNotice(`项目已创建，但素材导入失败：${String(error)}`);
      }
    } catch (error) {
      setNotice(`创建失败：${String(error)}`);
    }
  }

  async function openExistingProject() {
    try {
      if (!isTauriRuntime()) return;
      const path = await open({
        multiple: false,
        filters: [{ name: "H3 项目", extensions: ["json"] }],
        title: "打开 project.h3.json",
      });
      if (!path) return;
      const loaded = await loadProject(path);
      setProject(loaded);
      setProjectPath(path);
      localStorage.setItem("h3-recent-project", path);
      await restoreJobsAndSession(path, loaded);
      setNotice("项目已打开");
    } catch (error) {
      setNotice(`打开失败：${String(error)}`);
    }
  }

  async function addAssets() {
    try {
      if (!projectPath && isTauriRuntime()) {
        pendingDroppedPaths.current = [];
        setPendingImportAfterCreate(true);
        setNewProjectName("");
        setNewProjectOpen(true);
        setNotice("先创建项目，完成后会自动继续导入素材");
        return;
      }
      if (!isTauriRuntime()) {
        document.getElementById("browser-assets")?.click();
        return;
      }
      await chooseAndImportAssets(projectPath);
    } catch (error) {
      setNotice(`导入失败：${String(error)}`);
    }
  }

  async function chooseAndImportAssets(targetProjectPath: string) {
    const paths = await open({
      multiple: true,
      title: "一次选择最多 9 张参考图片，或视频和音频",
      filters: [
        { name: "素材", extensions: ["png", "jpg", "jpeg", "webp", "mp4", "mov", "wav", "mp3", "m4a"] },
      ],
    });
    if (!paths) return;
    await importPathsIntoProject(targetProjectPath, paths);
  }

  async function importPathsIntoProject(targetProjectPath: string, paths: string[]) {
    const imported = await importProjectAssets(targetProjectPath, paths);
    attachAssets(imported);
  }

  function attachAssets(imported: ProjectAsset[]) {
    setProject((current) => {
      const images = imported.filter((asset) => asset.kind === "image").slice(0, 9);
      const existingIds = current.nodes.find((node) => node.id === "references")?.data.assetIds ?? [];
      return {
        ...current,
        assets: [...current.assets, ...imported],
        nodes: updateNodeData(current.nodes, "references", {
          assetIds: [...existingIds, ...images.map((asset) => asset.id)].slice(0, 9),
          status: images.length ? "ready" : "idle",
        }),
      };
    });
    setSelectedNodeId("references");
    setNotice(`已导入 ${imported.length} 个素材`);
  }

  function addBrowserFiles(files: FileList | null) {
    if (!files) return;
    const assets: ProjectAsset[] = [...files].map((file) => ({
      id: crypto.randomUUID(),
      name: file.name,
      kind: file.type.startsWith("video") ? "video" : file.type.startsWith("audio") ? "audio" : "image",
      relativePath: `Assets/${file.name}`,
      bytes: file.size,
      previewUrl: URL.createObjectURL(file),
    }));
    attachAssets(assets);
  }

  async function queueJobs() {
    try {
      if (!projectPath) throw new Error("请先创建或打开一个项目");
      const compiled = compileJobs(project);
      setJobs((current) => [...current, ...compiled]);
      await Promise.all(compiled.map((job) => writeJob(projectPath, job)));
      setNotice(`已加入 ${compiled.length} 个不可变任务快照`);
    } catch (error) {
      console.error("无法编译 H3 任务快照", error);
      setNotice(String(error));
    }
  }

  async function prepareGpuStart() {
    setBusy(true);
    try {
      if (!isTauriRuntime()) throw new Error("请在独立 Mac 应用中启动真实 GPU");
      const next = jobs.find((job) => ["queued", "paused"].includes(job.status));
      if (!next) throw new Error("队列中没有待生成任务");
      const liveQuote = await executor.quote(next, project.settings);
      setQuote(liveQuote);
      setGpuConfirmOpen(true);
    } catch (error) {
      setNotice(`无法获取 GPU 实时报价：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function confirmStartGpu() {
    if (!quote?.available) return;
    setBusy(true);
    try {
      setGpuConfirmOpen(false);
      const created = await executor.ensureSession(project.settings, jobs, quote.dataCenterId);
      const resumed = jobs.map((job) => job.status === "paused" ? { ...job, status: "queued" as const, updatedAt: new Date().toISOString() } : job);
      if (resumed.some((job, index) => job !== jobs[index])) {
        setJobs(resumed);
        await Promise.all(resumed.filter((job) => job.status === "queued").map((job) => writeJob(projectPath, job)));
      }
      setSession(created);
      setNotice(`${created.dataCenterId ?? "许可地区"} 的 RunPod 正在启动；准备完成后会自动上传队列素材`);
    } catch (error) {
      setNotice(`GPU 启动失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function chooseGpu(gpu: string) {
    const next = jobs.find((job) => ["queued", "paused"].includes(job.status));
    if (!next) return;
    setBusy(true);
    try {
      const nextSettings = { ...project.settings, preferredGpu: gpu };
      const liveQuote = await executor.quote(next, nextSettings);
      setProject((current) => ({ ...current, settings: { ...current.settings, preferredGpu: gpu } }));
      setQuote(liveQuote);
    } catch (error) {
      setNotice(`备选 GPU 报价失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function stopGpu() {
    if (!session) return;
    setBusy(true);
    try {
      await executor.terminate(session);
      setSession(null);
      setJobs((current) => current.map((job) => job.status === "queued" ? { ...job, status: "paused" } : job));
      setNotice("GPU 已销毁，未开始任务已暂停；云端不再保留磁盘");
    } catch (error) {
      setNotice(`终止失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveRunPodKey() {
    try {
      setBusy(true);
      if (apiKey.trim()) {
        await invoke("set_secret", { account: "runpod-api-key", secret: apiKey.trim() });
        setApiKey("");
      } else if (!runpodConnected) {
        throw new Error("请输入 RunPod API Key");
      }
      const report = await invoke<{
        connected: boolean;
        clientBalance: number;
        currentSpendPerHour: number;
        activePods: number;
        allowedRegionsWithStock: number;
      }>("runpod_test_connection");
      setRunpodConnected(report.connected);
      const summary = `连接正常 · 余额 $${report.clientBalance.toFixed(2)} · 运行中 Pod ${report.activePods} 个 · 许可地区有库存 ${report.allowedRegionsWithStock} 处`;
      setConnectionReport(summary);
      setNotice(summary);
    } catch (error) {
      setRunpodConnected(false);
      setConnectionReport(`连接失败：${String(error)}`);
      setNotice(`RunPod 连接失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  const selectedAssetIds = project.nodes.find((node) => node.id === "references")?.data.assetIds ?? [];
  const selectedAssets = selectedAssetIds.map((id) => project.assets.find((asset) => asset.id === id)).filter(Boolean) as ProjectAsset[];
  const selectedPreset = QUALITY_PRESETS[(project.nodes.find((node) => node.id === "generate")?.data.presetId ?? "daily") as QualityPresetId];
  const headlineCost = quote?.estimatedUsd;
  const hasRunnableJobs = jobs.some((job) => ["queued", "paused"].includes(job.status));
  const lastCost = [...jobs].reverse().find((job) => job.cost?.breakdown)?.cost;
  const sessionEstimate = session ? estimatedCost(session.hourlyRateUsd || quote?.hourlyRateUsd || 0, Math.max(0, (Date.now() - new Date(session.startedAt).getTime()) / 1000)) : 0;

  const themeStyle = {
    "--app-bg": theme.base,
    "--green": theme.accent,
    "--green-deep": `color-mix(in srgb, ${theme.accent} 42%, ${theme.base})`,
    "--line": `color-mix(in srgb, ${theme.base}, white 13%)`,
    "--surface": `color-mix(in srgb, ${theme.base}, white 4%)`,
    "--surface-2": `color-mix(in srgb, ${theme.base}, white 8%)`,
  } as CSSProperties;

  return (
    <div className="studio-shell" style={themeStyle}>
      <header className="topbar">
        <div className="brand"><span>H3</span><div><strong>Production Studio</strong><small>Powered by MiniMax H3</small></div></div>
        <div className="project-title">
          <button onClick={openExistingProject}><FolderOpen size={15} />{project.name}<ChevronDown size={14} /></button>
          <span>{notice}</span>
        </div>
        <div className="session-controls">
          <div className={`cloud-state ${session ? "is-live" : ""}`}>
            <Cloud size={15} /><span>{session ? `GPU · ${session.dataCenterId ?? "运行中"}` : "GPU 未启动"}</span>
            {idleLeft != null && <b>{Math.floor(idleLeft / 60)}:{String(idleLeft % 60).padStart(2, "0")} 后关闭</b>}
          </div>
          {session ? (
            <button className="button button--danger" onClick={stopGpu} disabled={busy}><Square size={14} />停止并销毁</button>
          ) : (
            <button className="button button--primary" onClick={prepareGpuStart} disabled={busy || !hasRunnableJobs}>
              {busy ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}启动 GPU
            </button>
          )}
          <button className={`icon-button ${paletteOpen ? "is-active" : ""}`} onClick={() => setPaletteOpen((open) => !open)} aria-label="打开色板" title="更换页面颜色"><Palette size={17} /></button>
          <button className="icon-button" onClick={() => setSettingsOpen(true)} aria-label="设置"><Settings2 size={17} /></button>
        </div>
      </header>

      <main className="workspace">
        <aside className="asset-rail">
          <div className="project-strip">
            <button className="current-project" onClick={openExistingProject} title="打开其他项目"><FolderOpen size={16} /><span><small>当前项目</small><strong>{project.name}</strong></span><ChevronDown size={14} /></button>
            <div className="project-actions"><button onClick={createNewProject}><Plus size={14} />新建</button><button onClick={openExistingProject}><FolderOpen size={14} />切换</button></div>
          </div>
          <div className="panel-heading"><div><small>PROJECT ASSETS</small><h2>素材</h2></div><button className="asset-add-button" onClick={addAssets} aria-label="导入素材"><Upload size={14} />导入</button></div>
          <input id="browser-assets" hidden type="file" multiple accept="image/*,video/*,audio/*" onChange={(event) => addBrowserFiles(event.target.files)} />
          <button
            className={draggingAssets ? "drop-zone is-dragging" : "drop-zone"}
            onClick={addAssets}
            onDragOver={(event) => { event.preventDefault(); setDraggingAssets(true); }}
            onDragLeave={() => setDraggingAssets(false)}
            onDrop={(event) => { event.preventDefault(); setDraggingAssets(false); addBrowserFiles(event.dataTransfer.files); }}
          ><Upload size={18} /><strong>{draggingAssets ? "松手导入素材" : "一次导入多图"}</strong><span>最多 9 张自动编号</span></button>
          <div className="asset-list">
            {project.assets.length === 0 && <div className="empty-assets"><Images size={24} /><p>素材会保存在项目的 Assets 文件夹</p></div>}
            {project.assets.map((asset, index) => (
              <button key={asset.id} className={selectedAssetIds.includes(asset.id) ? "asset is-used" : "asset"} onClick={() => {
                if (asset.kind !== "image") return;
                const next = selectedAssetIds.includes(asset.id)
                  ? selectedAssetIds.filter((id) => id !== asset.id)
                  : [...selectedAssetIds, asset.id].slice(0, 9);
                patchNode("references", { assetIds: next, status: next.length ? "ready" : "idle" });
              }}>
                <div className="asset-thumb">
                  {asset.previewUrl ? <img src={asset.previewUrl} alt="" /> : asset.kind === "video" ? <Video size={20} /> : <Images size={20} />}
                  {selectedAssetIds.includes(asset.id) && <b>{selectedAssetIds.indexOf(asset.id) + 1}</b>}
                </div>
                <span>{asset.name}</span><small>{asset.kind === "image" ? `Picture ${index + 1}` : asset.kind}</small>
              </button>
            ))}
          </div>
          <div className="storage-note"><HardDrive size={15} /><span><strong>本地＋iCloud</strong>云端实例不保存长期数据</span></div>
        </aside>

        <section className="canvas-wrap">
          <div className="canvas-toolbar">
            <span>镜头画布</span><i />
            <button onClick={createNewProject}><Plus size={14} />新项目</button>
            <button onClick={openExistingProject}><FolderOpen size={14} />切换项目</button>
            <button disabled={!projectPath} onClick={() => saveProject(projectPath, project)}><Save size={14} />保存</button>
          </div>
          <ReactFlow
            nodes={project.nodes}
            edges={project.edges}
            nodeTypes={H3_NODE_TYPES}
            onNodesChange={updateNodes}
            onEdgesChange={updateEdges}
            onConnect={connect}
            onNodeClick={(_, node) => setSelectedNodeId(node.id)}
            fitView
            minZoom={0.35}
            maxZoom={1.5}
            colorMode="dark"
          >
            <Background color="#30463e" gap={28} size={1} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeColor="#587d6c" maskColor="rgba(9,18,15,.68)" />
          </ReactFlow>
          <div className="canvas-hint">拖动画布 · 滚轮缩放 · 点击节点编辑</div>
        </section>

        <aside className="inspector">
          <div className="panel-heading"><div><small>INSPECTOR</small><h2>{selectedNode?.data.label ?? "设置"}</h2></div></div>
          {selectedNode?.id === "references" && (
            <div className="inspector-body">
              <label>参考图片 <span>{selectedAssets.length}/9</span></label>
              <div className="reference-order">
                {selectedAssets.map((asset, index) => <div key={asset.id}><b>{index + 1}</b><span>{asset.name}</span><button onClick={() => patchNode("references", { assetIds: selectedAssetIds.filter((id) => id !== asset.id) })}><X size={13} /></button></div>)}
              </div>
              <button className="button button--quiet" onClick={addAssets}><Plus size={14} />继续添加</button>
              <p className="helper">提示词会按这里的顺序使用 &lt;Picture 1&gt; 到 &lt;Picture 9&gt;。</p>
            </div>
          )}
          {selectedNode?.id === "prompt" && (
            <div className="inspector-body">
              <label htmlFor="prompt">镜头提示词</label>
              <textarea id="prompt" value={String(selectedNode.data.prompt ?? "")} onChange={(event) => patchNode("prompt", { prompt: event.target.value, status: event.target.value.trim() ? "ready" : "idle" })} placeholder="人物动作、镜头运动、环境变化、对白和声音…" />
              <p className="helper">建议按时间分段描述动作，并明确要求一个连续镜头。</p>
            </div>
          )}
          {selectedNode?.id === "generate" && (
            <div className="inspector-body">
              <label>画质预设</label>
              <div className="preset-list">
                {Object.values(QUALITY_PRESETS).map((preset) => (
                  <button key={preset.id} className={preset.id === selectedPreset.id ? "preset is-active" : "preset"} onClick={() => {
                    patchNode("generate", { presetId: preset.id });
                    patchNode("upscale", { enabled: preset.upscale });
                  }}>
                    <span><strong>{preset.label}</strong><small>{preset.width}×{preset.height}{preset.upscale ? " → 1080×1920" : ""}</small></span>
                    <b>{elapsedLabel(preset.estimatedSeconds)}</b>
                  </button>
                ))}
              </div>
              <label htmlFor="seeds">种子 <span>逗号分隔可批量生成</span></label>
              <input id="seeds" defaultValue="-1" onBlur={(event) => patchNode("generate", { seeds: event.target.value.split(",").map(Number).filter(Number.isFinite).slice(0, 10) })} />
              <div className="quality-facts"><span>25 步</span><span>beta</span><span>24 fps</span><span>CRF 14</span></div>
            </div>
          )}
          {selectedNode?.id === "upscale" && (
            <div className="inspector-body"><label className="toggle"><input type="checkbox" checked={Boolean(selectedNode.data.enabled)} onChange={(event) => patchNode("upscale", { enabled: event.target.checked })} /><span />使用 SeedVR2 3B FP8</label><p className="helper">增强版与原片并存。人物细节发生变化时可关闭，不把普通插值标记为高清修复。</p></div>
          )}
          {selectedNode?.id === "export" && (
            <div className="inspector-body"><label>主输出</label><div className="export-spec"><b>MP4 · H.264 10-bit</b><span>24fps · AAC · CRF 14</span></div><label className="toggle"><input type="checkbox" defaultChecked /><span />同时导出 8-bit 兼容版</label></div>
          )}
          <div className="estimate-card"><div><Gauge size={16} /><span>本次估算</span></div><strong>{headlineCost == null ? "--" : `$${headlineCost.toFixed(2)}`}</strong><small>{quote?.source === "live" ? "按 RunPod 实时单价" : "加入任务后获取实时价格"}</small>{lastCost?.breakdown && <div className="phase-costs">{(["startup", "upload", "generation", "enhancement", "download"] as const).map((phase) => <span key={phase}><i>{{ startup: "启动", upload: "上传", generation: "生成", enhancement: "增强", download: "下载" }[phase]}</i><b>${lastCost.breakdown![phase].estimatedUsd.toFixed(3)}</b></span>)}</div>}</div>
          <button className="queue-button" disabled={!projectPath} onClick={queueJobs}><Plus size={17} />加入生成队列</button>
        </aside>
      </main>

      <footer className="queue-dock">
        <div className="queue-heading"><div><small>PRODUCTION QUEUE</small><h2>生成队列 <b>{jobs.length}</b></h2></div><button onClick={() => setJobs((current) => clearPendingJobs(current))} disabled={!jobs.some((job) => ["queued", "paused"].includes(job.status))}><Trash2 size={14} />清除未开始</button></div>
        <div className="queue-track">
          {jobs.length === 0 && <div className="queue-empty"><Play size={16} />画布准备好后，将任务加入这里；GPU 只在开始执行时启动。</div>}
          {jobs.map((job, index) => <article key={job.id} className={`job job--${job.status}`}><span className="job-index">{String(index + 1).padStart(2, "0")}</span><div><strong>{job.label}</strong><small>{job.preset.width}×{job.preset.height} · seed {job.seed}</small></div><b>{statusLabel(job.status)}</b>{job.status === "failed" && <AlertTriangle size={14} />}</article>)}
        </div>
        <div className="queue-cost"><CircleDollarSign size={16} /><span>{session ? "会话当前估算" : "会话软提醒"}</span><strong>${session ? sessionEstimate.toFixed(2) : project.settings.sessionSoftBudgetUsd.toFixed(2)}</strong><small>{session ? "含启动、生成与空闲；账单可能延迟" : "不会中断正在生成的任务"}</small></div>
      </footer>

      {paletteOpen && <div className="palette-scrim" onMouseDown={() => setPaletteOpen(false)}><section className="palette-popover" onMouseDown={(event) => event.stopPropagation()}><header><div><small>APPEARANCE</small><h2>页面色板</h2></div><button onClick={() => setPaletteOpen(false)} aria-label="关闭色板"><X size={17} /></button></header><div className="theme-presets">{THEME_PRESETS.map((preset) => <button key={preset.id} className={theme.id === preset.id ? "is-active" : ""} aria-pressed={theme.id === preset.id} onClick={() => setTheme(preset)}><i style={{ background: preset.base }}><b style={{ background: preset.accent }} /></i><span>{preset.name}</span></button>)}</div><div className="custom-colors"><label><span>页面底色</span><input type="color" value={theme.base} aria-label="自定义页面底色" onChange={(event) => setTheme((current) => ({ ...current, id: "custom", name: "自定义", base: event.target.value }))} /></label><label><span>强调颜色</span><input type="color" value={theme.accent} aria-label="自定义强调颜色" onChange={(event) => setTheme((current) => ({ ...current, id: "custom", name: "自定义", accent: event.target.value }))} /></label></div><p>只改变工作台外观，不影响素材和生成结果。</p></section></div>}
      {settingsOpen && (
        <div className="modal-backdrop" onMouseDown={() => setSettingsOpen(false)}>
          <section className="settings-modal" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><small>CLOUD CONTROL</small><h2>连接 RunPod</h2></div><button onClick={() => setSettingsOpen(false)}><X size={18} /></button></header>
            <div className="settings-grid">
              <label className="full">RunPod 专用 API Key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={runpodConnected ? "已安全保存在 macOS 钥匙串；留空可直接复测" : "只保存在这台 Mac 的钥匙串"} /></label>
              <div className={`full connection-state ${runpodConnected ? "is-connected" : ""}`}><Cloud size={16} /><span><strong>{runpodConnected ? "已保存本地密钥" : "尚未连接"}</strong><small>{connectionReport || "保存后会验证余额读取、GPU 报价和 Pod 管理权限"}</small></span></div>
              <label>空闲自动销毁（分钟）<input type="number" value={project.settings.idleTerminateMinutes} onChange={(event) => setProject((current) => ({ ...current, settings: { ...current.settings, idleTerminateMinutes: Math.max(1, Number(event.target.value)) } }))} /></label>
              <label>会话费用软提醒（USD）<input type="number" step="0.5" value={project.settings.sessionSoftBudgetUsd} onChange={(event) => setProject((current) => ({ ...current, settings: { ...current.settings, sessionSoftBudgetUsd: Math.max(0, Number(event.target.value)) } }))} /></label>
              <label>单任务超时（分钟）<input type="number" value={project.settings.jobTimeoutMinutes} onChange={(event) => setProject((current) => ({ ...current, settings: { ...current.settings, jobTimeoutMinutes: Math.max(5, Number(event.target.value)) } }))} /></label>
              <div className="full fixed-runtime"><span>云端执行器</span><strong>固定版本镜像 · SHA256 校验</strong><small>普通使用无需填写镜像地址；模型与素材不包含在镜像中。</small></div>
              <label className="full toggle notification-toggle"><input type="checkbox" checked={project.settings.notificationsEnabled} onChange={(event) => void setNotifications(event.target.checked)} /><span />任务完成、失败和 GPU 自动关闭时发送系统通知</label>
            </div>
            <div className="warning"><AlertTriangle size={16} /><span>仅使用澳洲、加拿大、日本、冰岛或挪威。空闲 30 分钟销毁，RunPod 另有 3 小时强制删除保护。</span></div>
            <footer><button className="button button--quiet" onClick={() => setSettingsOpen(false)}>完成</button><button className="button button--primary" disabled={busy} onClick={saveRunPodKey}>{busy ? <LoaderCircle size={14} className="spin" /> : <Cloud size={14} />}{runpodConnected && !apiKey.trim() ? "测试已有连接" : "保存并测试"}</button></footer>
          </section>
        </div>
      )}
      {newProjectOpen && <div className="modal-backdrop" onMouseDown={closeNewProjectModal}><section className="settings-modal project-modal" onMouseDown={(event) => event.stopPropagation()}><header><div><small>NEW PROJECT</small><h2>{pendingImportAfterCreate ? "先创建项目，再导入素材" : "创建 H3 项目"}</h2></div><button onClick={closeNewProjectModal}><X size={18} /></button></header><div className="project-create-body"><label htmlFor="project-name">项目名称</label><input id="project-name" autoFocus value={newProjectName} placeholder="例如：板栗 停车场" onChange={(event) => setNewProjectName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void confirmCreateProject(); }} /><div className="icloud-destination"><Cloud size={17} /><span><strong>项目位置由 H3 Studio 自动管理</strong><small>{pendingDroppedPaths.current.length > 0 ? `创建后自动导入刚才拖入的 ${pendingDroppedPaths.current.length} 个素材` : pendingImportAfterCreate ? "创建后自动打开素材选择，不需要重新操作" : "优先保存到 iCloud Drive；未启用时安全保存在本机"}</small></span></div></div><footer><button className="button button--quiet" onClick={closeNewProjectModal}>取消</button><button className="button button--primary" disabled={!newProjectName.trim()} onClick={() => confirmCreateProject()}>创建项目</button></footer></section></div>}
      {gpuConfirmOpen && quote && <div className="modal-backdrop" onMouseDown={() => setGpuConfirmOpen(false)}><section className="settings-modal gpu-confirm-modal" onMouseDown={(event) => event.stopPropagation()}><header><div><small>LIVE RUNPOD QUOTE</small><h2>确认启动按需 GPU</h2></div><button onClick={() => setGpuConfirmOpen(false)}><X size={18} /></button></header><div className="gpu-quote-body"><div className={quote.available ? "gpu-primary is-available" : "gpu-primary is-unavailable"}><span><strong>{quote.gpu.replace("NVIDIA ", "")}</strong><small>{quote.available ? `${quote.dataCenterId} · ${quote.dataCenterLocation ?? "许可地区"} · 库存 ${quote.stockStatus ?? "可用"}` : "许可地区当前缺货，不会自动购买"}</small></span><b>${quote.hourlyRateUsd.toFixed(2)}<small>/小时</small></b></div><div className="quote-summary"><span>首个任务预计</span><strong>${quote.estimatedUsd.toFixed(2)}</strong><small>RunPod 最终账单可能略有延迟</small></div>{quote.alternatives?.length ? <div className="gpu-alternatives"><label>备选 GPU（必须手动选择）</label>{quote.alternatives.map((option) => <button key={option.gpu} disabled={!option.available || busy} onClick={() => chooseGpu(option.gpu)}><span>{option.gpu.replace("NVIDIA ", "")}<small>{option.available ? `${option.dataCenterId} 可用` : "许可地区缺货"}</small></span><b>${option.hourlyRateUsd.toFixed(2)}/小时</b></button>)}</div> : null}<div className="billing-warning"><CircleDollarSign size={17} /><span><strong>点击确认后即开始按秒计费</strong><small>空闲 30 分钟自动销毁；创建后 3 小时强制删除；不创建付费卷。</small></span></div></div><footer><button className="button button--quiet" onClick={() => setGpuConfirmOpen(false)}>取消</button><button className="button button--primary" disabled={!quote.available || !quote.dataCenterId || busy} onClick={confirmStartGpu}>{busy ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}确认并启动</button></footer></section></div>}
    </div>
  );
}
