import type { Edge } from "@xyflow/react";
import {
  QUALITY_PRESETS,
  type H3CanvasNode,
  type H3Project,
  type JobSnapshot,
  type QualityPresetId,
} from "../types";

function connectedToGenerate(nodes: H3CanvasNode[], edges: Edge[]): H3CanvasNode[] {
  const wanted = new Set<string>(["generate"]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of edges) {
      if (wanted.has(edge.target) && !wanted.has(edge.source)) {
        wanted.add(edge.source);
        changed = true;
      }
    }
  }
  return nodes.filter((node) => wanted.has(node.id));
}

export function compileJobs(project: H3Project): JobSnapshot[] {
  const promptNode = project.nodes.find((node) => node.type === "prompt");
  const referenceNode = project.nodes.find((node) => node.type === "mediaStack");
  const generateNode = project.nodes.find((node) => node.type === "generate");
  const prompt = String(promptNode?.data.prompt ?? "").trim();
  if (!prompt) throw new Error("请先填写镜头提示词");

  const presetId = (generateNode?.data.presetId ?? "daily") as QualityPresetId;
  const preset = QUALITY_PRESETS[presetId];
  if (!preset) throw new Error("画质预设无效");

  const assetIds = (referenceNode?.data.assetIds ?? []).slice(0, 9);
  const assetMap = new Map(project.assets.map((asset) => [asset.id, asset]));
  const missing = assetIds.filter((id) => !assetMap.has(id));
  if (missing.length) throw new Error("有参考素材已从项目中移除，请重新选择");

  const rawSeeds = generateNode?.data.seeds?.length ? generateNode.data.seeds : [-1];
  const seeds = rawSeeds.map((seed) =>
    seed < 0 ? Math.floor(Math.random() * 2 ** 32) : Math.trunc(seed),
  );
  const now = new Date().toISOString();
  const graphNodes = structuredClone(connectedToGenerate(project.nodes, project.edges));
  const graphNodeIds = new Set(graphNodes.map((node) => node.id));
  const graphEdges = structuredClone(
    project.edges.filter(
      (edge) => graphNodeIds.has(edge.source) && graphNodeIds.has(edge.target),
    ),
  );

  return seeds.map((seed, index) => ({
    schemaVersion: 1,
    id: crypto.randomUUID(),
    projectId: project.id,
    createdAt: now,
    updatedAt: now,
    status: "queued",
    label: seeds.length > 1 ? `${preset.label} · 变体 ${index + 1}` : preset.label,
    prompt,
    assetIds: [...assetIds],
    assetPaths: assetIds.map((id) => assetMap.get(id)!.relativePath),
    preset: structuredClone(preset),
    seed,
    graphSnapshot: { nodes: graphNodes, edges: graphEdges },
  }));
}

export function clearPendingJobs(jobs: JobSnapshot[]): JobSnapshot[] {
  return jobs.filter((job) => !["queued", "paused"].includes(job.status));
}

export function estimatedCost(hourlyRate: number, seconds: number): number {
  if (!Number.isFinite(hourlyRate) || hourlyRate < 0) return 0;
  return Math.round((hourlyRate * seconds * 100) / 3600) / 100;
}

export function shouldTerminateIdle(
  jobs: JobSnapshot[],
  idleSinceMs: number | null,
  nowMs: number,
  idleMinutes: number,
): boolean {
  if (idleSinceMs == null || idleMinutes <= 0) return false;
  const active = jobs.some((job) =>
    ["queued", "starting", "uploading", "running", "enhancing", "downloading"].includes(
      job.status,
    ),
  );
  return !active && nowMs - idleSinceMs >= idleMinutes * 60_000;
}
