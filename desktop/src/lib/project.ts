import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import type { Edge } from "@xyflow/react";
import type { H3CanvasNode, H3Project, JobSnapshot, ProjectAsset } from "../types";

export const DEFAULT_SETTINGS: H3Project["settings"] = {
  idleTerminateMinutes: 30,
  sessionSoftBudgetUsd: 2,
  jobTimeoutMinutes: 45,
  preferredGpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition",
  executor: "pod",
  runpodImage: "ghcr.io/replace-me/h3-production-worker:latest",
  runpodProxyPort: 8000,
  serverlessEndpointId: "",
  serverlessHourlyRateUsd: 0,
  notificationsEnabled: false,
};

export const DEFAULT_NODES: H3CanvasNode[] = [
  {
    id: "references",
    type: "mediaStack",
    position: { x: 60, y: 145 },
    data: { label: "多图参考", assetIds: [], status: "idle" },
  },
  {
    id: "prompt",
    type: "prompt",
    position: { x: 340, y: 82 },
    data: { label: "镜头提示词", prompt: "", status: "idle" },
  },
  {
    id: "generate",
    type: "generate",
    position: { x: 625, y: 145 },
    data: {
      label: "MiniMax H3",
      presetId: "daily",
      seeds: [-1],
      status: "ready",
    },
  },
  {
    id: "upscale",
    type: "upscale",
    position: { x: 910, y: 74 },
    data: { label: "高清增强", enabled: false, status: "idle" },
  },
  {
    id: "export",
    type: "export",
    position: { x: 910, y: 286 },
    data: { label: "成片输出", status: "idle" },
  },
];

export const DEFAULT_EDGES: Edge[] = [
  { id: "references-generate", source: "references", target: "generate" },
  { id: "prompt-generate", source: "prompt", target: "generate" },
  { id: "generate-upscale", source: "generate", target: "upscale" },
  { id: "upscale-export", source: "upscale", target: "export" },
];

export function newProject(name = "未命名项目"): H3Project {
  const now = new Date().toISOString();
  return {
    schemaVersion: 1,
    id: crypto.randomUUID(),
    name,
    createdAt: now,
    updatedAt: now,
    nodes: structuredClone(DEFAULT_NODES),
    edges: structuredClone(DEFAULT_EDGES),
    assets: [],
    settings: { ...DEFAULT_SETTINGS },
  };
}

function browserProjectKey(path: string) {
  return `h3-project:${path}`;
}

export function isTauriRuntime(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

export async function saveProject(path: string, project: H3Project): Promise<void> {
  const payload = {
    ...project,
    updatedAt: new Date().toISOString(),
    assets: project.assets.map(({ previewUrl: _previewUrl, ...asset }) => asset),
  };
  if (isTauriRuntime()) {
    await invoke("save_project", { projectPath: path, project: payload });
  } else {
    localStorage.setItem(browserProjectKey(path), JSON.stringify(payload));
  }
}

export async function loadProject(path: string): Promise<H3Project> {
  if (isTauriRuntime()) {
    const project = await invoke<H3Project>("load_project", { projectPath: path });
    return {
      ...project,
      settings: { ...DEFAULT_SETTINGS, ...project.settings },
      assets: project.assets.map((asset) => ({
        ...asset,
        previewUrl: asset.previewUrl ? convertFileSrc(asset.previewUrl) : undefined,
      })),
    };
  }
  const raw = localStorage.getItem(browserProjectKey(path));
  if (!raw) throw new Error("没有找到这个项目");
  return JSON.parse(raw) as H3Project;
}

export async function createProject(parentDir: string, name: string) {
  const project = newProject(name);
  if (isTauriRuntime()) {
    return invoke<{ projectPath: string; project: H3Project }>("create_project", {
      parentDir,
      project,
    });
  }
  const projectPath = `browser://${project.id}/project.h3.json`;
  await saveProject(projectPath, project);
  return { projectPath, project };
}

export async function importProjectAssets(
  projectPath: string,
  sources: string[],
): Promise<ProjectAsset[]> {
  if (!isTauriRuntime()) return [];
  const assets = await invoke<ProjectAsset[]>("import_assets", { projectPath, sources });
  return assets.map((asset) => ({
    ...asset,
    previewUrl: asset.previewUrl ? convertFileSrc(asset.previewUrl) : undefined,
  }));
}

export async function writeJob(projectPath: string, job: unknown): Promise<void> {
  if (isTauriRuntime()) {
    await invoke("write_job", { projectPath, job });
    return;
  }
  const parsed = job as { id: string };
  localStorage.setItem(`h3-job:${projectPath}:${parsed.id}`, JSON.stringify(job));
}

export async function loadJobs(projectPath: string): Promise<JobSnapshot[]> {
  if (isTauriRuntime()) {
    return invoke<JobSnapshot[]>("load_jobs", { projectPath });
  }
  const prefix = `h3-job:${projectPath}:`;
  const jobs: JobSnapshot[] = [];
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index);
    if (key?.startsWith(prefix)) {
      jobs.push(JSON.parse(localStorage.getItem(key)!) as JobSnapshot);
    }
  }
  return jobs.sort((left, right) => left.createdAt.localeCompare(right.createdAt));
}

export function updateNodeData(
  nodes: H3CanvasNode[],
  nodeId: string,
  patch: Partial<H3CanvasNode["data"]>,
): H3CanvasNode[] {
  return nodes.map((node) =>
    node.id === nodeId ? { ...node, data: { ...node.data, ...patch } } : node,
  );
}
