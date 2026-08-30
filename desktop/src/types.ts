import type { Edge, Node } from "@xyflow/react";

export type AssetKind = "image" | "video" | "audio";

export interface ProjectAsset {
  id: string;
  name: string;
  kind: AssetKind;
  relativePath: string;
  bytes?: number;
  sha256?: string;
  previewUrl?: string;
}

export type QualityPresetId = "preview" | "daily" | "native" | "delivery";

export interface QualityPreset {
  id: QualityPresetId;
  label: string;
  width: number;
  height: number;
  durationSeconds: number;
  frames: number;
  steps: number;
  estimatedSeconds: number;
  upscale: boolean;
  outputWidth?: number;
  outputHeight?: number;
}

export const QUALITY_PRESETS: Record<QualityPresetId, QualityPreset> = {
  preview: {
    id: "preview",
    label: "快速预览",
    width: 544,
    height: 960,
    durationSeconds: 15.08,
    frames: 362,
    steps: 25,
    estimatedSeconds: 579,
    upscale: false,
  },
  daily: {
    id: "daily",
    label: "日常最佳",
    width: 640,
    height: 1152,
    durationSeconds: 15.08,
    frames: 362,
    steps: 25,
    estimatedSeconds: 1009,
    upscale: false,
  },
  native: {
    id: "native",
    label: "极致原生",
    width: 768,
    height: 1344,
    durationSeconds: 15.08,
    frames: 362,
    steps: 25,
    estimatedSeconds: 1800,
    upscale: false,
  },
  delivery: {
    id: "delivery",
    label: "交付高清",
    width: 640,
    height: 1152,
    durationSeconds: 15.08,
    frames: 362,
    steps: 25,
    estimatedSeconds: 1500,
    upscale: true,
    outputWidth: 1080,
    outputHeight: 1920,
  },
};

export interface CanvasNodeData extends Record<string, unknown> {
  label: string;
  assetIds?: string[];
  prompt?: string;
  presetId?: QualityPresetId;
  seeds?: number[];
  enabled?: boolean;
  status?: "idle" | "ready" | "running" | "done" | "error";
}

export type H3CanvasNode = Node<CanvasNodeData>;

export interface H3ProjectSettings {
  idleTerminateMinutes: number;
  sessionSoftBudgetUsd: number;
  jobTimeoutMinutes: number;
  preferredGpu: string;
  executor: "pod" | "serverless";
  runpodImage: string;
  runpodProxyPort: number;
  serverlessEndpointId: string;
  serverlessHourlyRateUsd: number;
  notificationsEnabled: boolean;
}

export interface H3Project {
  schemaVersion: 1;
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
  nodes: H3CanvasNode[];
  edges: Edge[];
  assets: ProjectAsset[];
  settings: H3ProjectSettings;
}

export type JobStatus =
  | "queued"
  | "paused"
  | "starting"
  | "uploading"
  | "running"
  | "enhancing"
  | "downloading"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface JobSnapshot {
  schemaVersion: 1;
  id: string;
  projectId: string;
  createdAt: string;
  updatedAt: string;
  status: JobStatus;
  label: string;
  prompt: string;
  assetIds: string[];
  assetPaths: string[];
  preset: QualityPreset;
  seed: number;
  graphSnapshot: { nodes: H3CanvasNode[]; edges: Edge[] };
  remote?: {
    executor: "pod" | "serverless";
    sessionId?: string;
    remoteJobId?: string;
    promptId?: string;
  };
  timing?: {
    taskStartedAt?: string;
    sessionStartedAt?: string;
    uploadStartedAt?: string;
    generationStartedAt?: string;
    enhancementStartedAt?: string;
    downloadStartedAt?: string;
    finishedAt?: string;
  };
  cost?: {
    hourlyRateUsd: number;
    estimatedUsd: number;
    providerReportedUsd?: number;
    breakdown?: Record<"startup" | "upload" | "generation" | "enhancement" | "download", {
      seconds: number;
      estimatedUsd: number;
    }>;
  };
  outputRelativePath?: string;
  error?: string;
}

export interface ExecutorQuote {
  gpu: string;
  available: boolean;
  hourlyRateUsd: number;
  estimatedSeconds: number;
  estimatedUsd: number;
  source: "live" | "cached";
  dataCenterId?: string;
  dataCenterLocation?: string;
  stockStatus?: string;
  alternatives?: Array<{
    gpu: string;
    available: boolean;
    hourlyRateUsd: number;
    dataCenterId?: string;
  }>;
}

export interface ExecutorSession {
  id: string;
  status: "starting" | "ready" | "terminating" | "terminated" | "error";
  gpu: string;
  hourlyRateUsd: number;
  startedAt: string;
  gatewayUrl?: string;
  dataCenterId?: string;
  dataCenterLocation?: string;
  hardTerminateAt?: string;
  error?: string;
}
