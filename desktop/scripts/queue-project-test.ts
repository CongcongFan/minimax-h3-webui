import { copyFile, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { compileJobs } from "../src/lib/queue";
import type { H3Project, QualityPresetId } from "../src/types";

const [, , projectPath, presetArg = "preview", seedArg = "20260830"] = process.argv;
const allowedPresets = new Set<QualityPresetId>(["preview", "daily", "native", "delivery"]);

if (!projectPath) throw new Error("请提供 project.h3.json 的绝对路径");
if (!allowedPresets.has(presetArg as QualityPresetId)) throw new Error(`不支持的预设：${presetArg}`);

const project = JSON.parse(await readFile(projectPath, "utf8")) as H3Project;
const generate = project.nodes.find((node) => node.id === "generate");
if (!generate) throw new Error("项目中没有 MiniMax H3 生成节点");

generate.data.presetId = presetArg as QualityPresetId;
generate.data.seeds = [Number(seedArg)];
const [job] = compileJobs(project);
const jobDirectory = join(dirname(projectPath), "Jobs", job.id);
const destination = join(jobDirectory, "job.json");
const temporary = join(jobDirectory, ".job.json.tmp");

await mkdir(jobDirectory, { recursive: true });
await writeFile(temporary, `${JSON.stringify(job, null, 2)}\n`, "utf8");
try {
  await copyFile(destination, `${destination}.h3studio-backup`);
} catch {
  // 新任务没有旧文件需要备份。
}
await rename(temporary, destination);

process.stdout.write(`${job.id}\n`);
