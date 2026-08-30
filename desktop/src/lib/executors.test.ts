import { describe, expect, it } from "vitest";
import { workloadProfiles } from "./executors";
import type { JobSnapshot } from "../types";

function job(assetPaths: string[], upscale = false): JobSnapshot {
  const now = new Date(0).toISOString();
  return {
    schemaVersion: 1,
    id: crypto.randomUUID(),
    projectId: "project",
    createdAt: now,
    updatedAt: now,
    status: "queued",
    label: "测试任务",
    prompt: "测试",
    assetIds: [],
    assetPaths,
    preset: {
      id: upscale ? "delivery" : "daily",
      label: "测试",
      width: 640,
      height: 1152,
      durationSeconds: 15.08,
      frames: 362,
      steps: 25,
      estimatedSeconds: 1000,
      upscale,
    },
    seed: 1,
    graphSnapshot: { nodes: [], edges: [] },
  };
}

describe("workloadProfiles", () => {
  it("多参考任务只下载 Ref2VA 和共享模型", () => {
    expect(workloadProfiles([job(["Assets/1.png"])])).toEqual(["ref2va"]);
  });

  it("混合队列合并 FL2VA、Ref2VA 和 SeedVR2", () => {
    expect(workloadProfiles([
      job([]),
      job(["Assets/1.png"], true),
    ])).toEqual(["fl2va", "ref2va", "seedvr2"]);
  });

  it("暂停任务仍属于即将恢复的工作负载", () => {
    const paused = { ...job([], true), status: "paused" as const };
    expect(workloadProfiles([paused])).toEqual(["fl2va", "seedvr2"]);
  });
});
