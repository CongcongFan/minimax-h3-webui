import { describe, expect, it } from "vitest";
import { newProject, updateNodeData } from "./project";
import { clearPendingJobs, compileJobs, estimatedCost, shouldTerminateIdle } from "./queue";
import type { JobSnapshot, ProjectAsset } from "../types";

function readyProject() {
  const project = newProject("测试项目");
  const assets: ProjectAsset[] = Array.from({ length: 10 }, (_, index) => ({
    id: `asset-${index + 1}`,
    name: `${index + 1}.png`,
    kind: "image",
    relativePath: `Assets/${index + 1}.png`,
  }));
  project.assets = assets;
  project.nodes = updateNodeData(project.nodes, "references", {
    assetIds: assets.map((asset) => asset.id),
  });
  project.nodes = updateNodeData(project.nodes, "prompt", {
    prompt: "<Picture 1> 走向镜头，连续镜头。",
  });
  project.nodes = updateNodeData(project.nodes, "generate", {
    presetId: "daily",
    seeds: [10, 11, 12],
  });
  return project;
}

describe("compileJobs", () => {
  it("一次生成多个不可变快照，并把参考图限制为 9 张", () => {
    const project = readyProject();
    const jobs = compileJobs(project);

    expect(jobs).toHaveLength(3);
    expect(jobs.map((job) => job.seed)).toEqual([10, 11, 12]);
    expect(jobs[0].assetIds).toHaveLength(9);
    expect(jobs[0].preset.width).toBe(640);
    expect(jobs[0].graphSnapshot.nodes.some((node) => node.id === "export")).toBe(false);

    project.nodes = updateNodeData(project.nodes, "prompt", { prompt: "后来修改" });
    expect(jobs[0].prompt).not.toBe("后来修改");
  });

  it("没有提示词时拒绝排队", () => {
    const project = readyProject();
    project.nodes = updateNodeData(project.nodes, "prompt", { prompt: "   " });
    expect(() => compileJobs(project)).toThrow("请先填写镜头提示词");
  });

  it("引用已经移除的素材时给出可操作错误", () => {
    const project = readyProject();
    project.assets = [];
    expect(() => compileJobs(project)).toThrow("有参考素材已从项目中移除");
  });
});

describe("queue helpers", () => {
  it("只清除未开始任务，不影响运行中或完成任务", () => {
    const base = compileJobs(readyProject())[0];
    const jobs = [
      { ...base, id: "queued", status: "queued" as const },
      { ...base, id: "paused", status: "paused" as const },
      { ...base, id: "running", status: "running" as const },
      { ...base, id: "done", status: "succeeded" as const },
    ];
    expect(clearPendingJobs(jobs).map((job) => job.id)).toEqual(["running", "done"]);
  });

  it("按实际小时价格估算费用", () => {
    expect(estimatedCost(2.11, 1009)).toBe(0.59);
    expect(estimatedCost(-1, 1000)).toBe(0);
  });

  it("仅在没有活动任务且空闲达到阈值时销毁", () => {
    const base = compileJobs(readyProject())[0];
    expect(shouldTerminateIdle([], 0, 30 * 60_000, 30)).toBe(true);
    expect(shouldTerminateIdle([{ ...base, status: "running" } as JobSnapshot], 0, 31 * 60_000, 30)).toBe(false);
    expect(shouldTerminateIdle([], null, 31 * 60_000, 30)).toBe(false);
  });
});
