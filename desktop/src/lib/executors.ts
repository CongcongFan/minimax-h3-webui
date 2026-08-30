import { invoke } from "@tauri-apps/api/core";
import type {
  ExecutorQuote,
  ExecutorSession,
  H3ProjectSettings,
  JobSnapshot,
} from "../types";
import { estimatedCost } from "./queue";
import { isTauriRuntime } from "./project";

export interface Executor {
  quote(job: JobSnapshot, settings?: H3ProjectSettings): Promise<ExecutorQuote>;
  ensureSession(settings: H3ProjectSettings, jobs: JobSnapshot[], dataCenterId?: string): Promise<ExecutorSession>;
  submit(session: ExecutorSession, job: JobSnapshot): Promise<string>;
  status(session: ExecutorSession, remoteJobId: string): Promise<RemoteJobStatus>;
  cancel(session: ExecutorSession, remoteJobId: string): Promise<void>;
  downloadArtifacts(
    session: ExecutorSession,
    remoteJobId: string,
    projectPath: string,
  ): Promise<string>;
  terminate(session: ExecutorSession): Promise<void>;
}

export interface RemoteJobStatus {
  status: string;
  progress?: number;
  stage?: string;
  error?: string;
  artifact?: string;
  rawArtifact?: string;
}

export interface GatewayHealth {
  status: string;
  stage?: string;
  profile?: string[];
  downloadedBytes?: number;
  totalBytes?: number;
  currentFile?: string;
  error?: string;
  comfy: boolean;
  h3Nodes: boolean;
  seedvr2Nodes?: boolean;
}

export function workloadProfiles(jobs: JobSnapshot[]): string[] {
  const profiles = new Set<string>();
  for (const job of jobs.filter((item) => ["queued", "paused"].includes(item.status))) {
    profiles.add(job.assetPaths.length ? "ref2va" : "fl2va");
    if (job.preset.upscale) profiles.add("seedvr2");
  }
  if (!profiles.has("ref2va") && !profiles.has("fl2va")) profiles.add("ref2va");
  return [...profiles].sort();
}

async function gateway<T>(
  session: ExecutorSession,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  if (!session.gatewayUrl) throw new Error("云端网关尚未就绪");
  const token = await invoke<string>("get_secret", {
    account: `gateway:${session.id}`,
  });
  const response = await fetch(`${session.gatewayUrl}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
}

export class PodExecutor implements Executor {
  async waitUntilReady(
    session: ExecutorSession,
    onProgress?: (health: GatewayHealth) => void,
    timeoutMs = 12 * 60_000,
  ): Promise<ExecutorSession> {
    const deadline = Date.now() + timeoutMs;
    let lastError = "云端仍在启动";
    while (Date.now() < deadline) {
      try {
        const health = await gateway<GatewayHealth>(
          session,
          "/health",
        );
        onProgress?.(health);
        if (health.status === "failed") {
          throw new Error(health.error || health.stage || "云端执行器启动失败");
        }
        if (health.status === "ready" && health.comfy && health.h3Nodes) {
          return { ...session, status: "ready" };
        }
        lastError = health.stage || "ComfyUI 正在载入模型节点";
      } catch (error) {
        lastError = String(error);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 4000));
    }
    throw new Error(`GPU 启动超时：${lastError}`);
  }

  diagnostics(session: ExecutorSession): Promise<Record<string, unknown>> {
    return gateway(session, "/v1/diagnostics");
  }

  async uploadProjectAssets(
    session: ExecutorSession,
    projectPath: string,
    relativePaths: string[],
  ): Promise<string[]> {
    if (!session.gatewayUrl) throw new Error("云端网关尚未就绪");
    const token = await invoke<string>("get_secret", { account: `gateway:${session.id}` });
    const uploaded: string[] = [];
    for (const relativePath of relativePaths) {
      const result = await invoke<{ path: string }>("upload_project_asset", {
        url: `${session.gatewayUrl}/v1/assets`,
        token,
        projectPath,
        relativePath,
      });
      uploaded.push(result.path);
    }
    return uploaded;
  }

  async quote(job: JobSnapshot, settings?: H3ProjectSettings): Promise<ExecutorQuote> {
    if (!isTauriRuntime()) {
      const hourlyRateUsd = 0;
      return {
        gpu: "RTX PRO 6000 Blackwell 96GB",
        available: false,
        hourlyRateUsd,
        estimatedSeconds: job.preset.estimatedSeconds,
        estimatedUsd: 0,
        source: "cached",
      };
    }
    const quote = await invoke<Omit<ExecutorQuote, "estimatedSeconds" | "estimatedUsd">>(
      "runpod_quote",
      { gpuName: settings?.preferredGpu ?? "NVIDIA RTX PRO 6000 Blackwell Server Edition" },
    );
    return {
      ...quote,
      estimatedSeconds: job.preset.estimatedSeconds,
      estimatedUsd: estimatedCost(quote.hourlyRateUsd, job.preset.estimatedSeconds),
    };
  }

  async ensureSession(
    settings: H3ProjectSettings,
    jobs: JobSnapshot[],
    dataCenterId?: string,
  ): Promise<ExecutorSession> {
    if (!isTauriRuntime()) throw new Error("浏览器预览不会启动真实 GPU");
    if (!dataCenterId) throw new Error("报价没有返回许可地区的数据中心");
    return invoke<ExecutorSession>("runpod_create_session", {
      settings,
      workloadProfiles: workloadProfiles(jobs),
      dataCenterId,
    });
  }

  async submit(session: ExecutorSession, job: JobSnapshot): Promise<string> {
    const result = await gateway<{ id: string }>(session, "/v1/jobs", {
      method: "POST",
      body: JSON.stringify(job),
    });
    return result.id;
  }

  status(session: ExecutorSession, remoteJobId: string): Promise<RemoteJobStatus> {
    return gateway(session, `/v1/jobs/${remoteJobId}`);
  }

  async cancel(session: ExecutorSession, remoteJobId: string): Promise<void> {
    await gateway(session, `/v1/jobs/${remoteJobId}/cancel`, { method: "POST" });
  }

  async downloadArtifacts(
    session: ExecutorSession,
    remoteJobId: string,
    projectPath: string,
  ): Promise<string> {
    if (!isTauriRuntime()) throw new Error("浏览器预览不能下载云端成片");
    const token = await invoke<string>("get_secret", { account: `gateway:${session.id}` });
    const remote = await this.status(session, remoteJobId);
    if (remote.rawArtifact && remote.rawArtifact !== remote.artifact) {
      await invoke<string>("download_artifact", {
        url: `${session.gatewayUrl}/v1/jobs/${remoteJobId}/artifact?variant=raw`,
        token,
        projectPath,
        jobId: `${remoteJobId}_raw`,
      });
    }
    return invoke<string>("download_artifact", {
      url: `${session.gatewayUrl}/v1/jobs/${remoteJobId}/artifact`,
      token,
      projectPath,
      jobId: remoteJobId,
    });
  }

  async terminate(session: ExecutorSession): Promise<void> {
    if (!isTauriRuntime()) return;
    await invoke("runpod_terminate_session", { podId: session.id });
  }
}

export class ServerlessExecutor implements Executor {
  constructor(
    private readonly endpointId: string,
    private readonly hourlyRateUsd = 0,
  ) {}

  private configured() {
    if (!this.endpointId) throw new Error("尚未配置 RunPod Serverless Endpoint ID");
  }

  async quote(job: JobSnapshot): Promise<ExecutorQuote> {
    this.configured();
    return {
      gpu: "RunPod Serverless H3 Worker",
      available: true,
      hourlyRateUsd: this.hourlyRateUsd,
      estimatedSeconds: job.preset.estimatedSeconds,
      estimatedUsd: estimatedCost(this.hourlyRateUsd, job.preset.estimatedSeconds),
      source: "cached",
    };
  }

  async ensureSession(): Promise<ExecutorSession> {
    this.configured();
    return {
      id: this.endpointId,
      status: "ready",
      gpu: "RunPod Serverless",
      hourlyRateUsd: this.hourlyRateUsd,
      startedAt: new Date().toISOString(),
    };
  }

  async submit(_session: ExecutorSession, job: JobSnapshot): Promise<string> {
    this.configured();
    const result = await invoke<{ id: string }>("serverless_run", {
      endpointId: this.endpointId,
      input: job,
    });
    return result.id;
  }

  async status(_session: ExecutorSession, remoteJobId: string): Promise<RemoteJobStatus> {
    this.configured();
    const response = await invoke<{
      status: string;
      error?: string;
      output?: { artifactUrl?: string; progress?: number; stage?: string };
    }>("serverless_status", {
      endpointId: this.endpointId,
      jobId: remoteJobId,
    });
    const mapped: Record<string, string> = {
      IN_QUEUE: "queued",
      IN_PROGRESS: "running",
      COMPLETED: "succeeded",
      FAILED: "failed",
      CANCELLED: "cancelled",
      TIMED_OUT: "failed",
    };
    return {
      status: mapped[response.status] ?? response.status.toLowerCase(),
      progress: response.output?.progress,
      stage: response.output?.stage,
      error: response.error,
      artifact: response.output?.artifactUrl,
    };
  }

  async cancel(_session: ExecutorSession, remoteJobId: string): Promise<void> {
    this.configured();
    await invoke("serverless_cancel", { endpointId: this.endpointId, jobId: remoteJobId });
  }

  async downloadArtifacts(
    _session: ExecutorSession,
    remoteJobId: string,
    projectPath: string,
  ): Promise<string> {
    const state = await this.status(_session, remoteJobId);
    if (!state.artifact) throw new Error("Serverless 任务没有返回带时效的 MP4 下载地址");
    return invoke<string>("download_public_artifact", {
      url: state.artifact,
      projectPath,
      jobId: remoteJobId,
    });
  }

  async terminate(): Promise<void> {
    // workers=(0,1) 的 Endpoint 在空闲后由 RunPod 自动缩容到零。
  }
}
