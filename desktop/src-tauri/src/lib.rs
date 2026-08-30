use chrono::{Duration, Utc};
use keyring::Entry;
use reqwest::header::AUTHORIZATION;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use tauri::Manager;
use uuid::Uuid;

const KEYCHAIN_SERVICE: &str = "studio.h3.production";
const RUNPOD_API: &str = "https://rest.runpod.io/v1";
const RUNPOD_GRAPHQL: &str = "https://api.runpod.io/graphql";
const SESSION_HARD_LIMIT_HOURS: i64 = 3;
const ALLOWED_DATA_CENTERS: [&str; 9] = [
    "OC-AU-1", "CA-MTL-1", "CA-MTL-2", "CA-MTL-3", "AP-JP-1", "EUR-IS-1", "EUR-IS-2", "EUR-IS-3",
    "EUR-NO-1",
];

fn entry(account: &str) -> Result<Entry, String> {
    Entry::new(KEYCHAIN_SERVICE, account).map_err(|error| error.to_string())
}

fn project_root(project_path: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(project_path);
    path.parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "项目路径无效".to_string())
}

fn safe_project_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|character| match character {
            '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => ' ',
            other => other,
        })
        .collect();
    let trimmed = cleaned.trim();
    if trimmed.is_empty() {
        "H3 项目".to_string()
    } else {
        trimmed.to_string()
    }
}

fn safe_file_component(value: &str) -> String {
    let cleaned: String = value
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
        .take(96)
        .collect();
    if cleaned.is_empty() {
        Uuid::new_v4().to_string()
    } else {
        cleaned
    }
}

fn atomic_json_write(path: &Path, value: &Value) -> Result<(), String> {
    let parent = path.parent().ok_or_else(|| "目标目录无效".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy(),
        Uuid::new_v4()
    ));
    let data = serde_json::to_vec_pretty(value).map_err(|error| error.to_string())?;
    {
        let mut file = fs::File::create(&temporary).map_err(|error| error.to_string())?;
        file.write_all(&data).map_err(|error| error.to_string())?;
        file.sync_all().map_err(|error| error.to_string())?;
    }
    if path.exists() {
        let backup = path.with_extension("json.h3studio-backup");
        fs::copy(path, backup).map_err(|error| error.to_string())?;
    }
    fs::rename(&temporary, path).map_err(|error| error.to_string())?;
    Ok(())
}

#[tauri::command]
fn create_project(parent_dir: String, project: Value) -> Result<Value, String> {
    let name = project
        .get("name")
        .and_then(Value::as_str)
        .map(safe_project_name)
        .unwrap_or_else(|| "H3 项目".to_string());
    let root = PathBuf::from(parent_dir).join(name);
    for folder in ["Assets", "Jobs", "Outputs"] {
        fs::create_dir_all(root.join(folder)).map_err(|error| error.to_string())?;
    }
    let path = root.join("project.h3.json");
    if path.exists() {
        return Err("同名项目已经存在，请更换项目名称".to_string());
    }
    atomic_json_write(&path, &project)?;
    Ok(json!({ "projectPath": path, "project": project }))
}

#[tauri::command]
fn default_project_parent() -> Result<String, String> {
    let home = dirs::home_dir().ok_or_else(|| "无法找到当前用户目录".to_string())?;
    let cloud_docs = home
        .join("Library")
        .join("Mobile Documents")
        .join("com~apple~CloudDocs");
    let projects = if cloud_docs.is_dir() {
        cloud_docs.join("H3 Projects")
    } else {
        dirs::data_dir()
            .ok_or_else(|| "无法找到应用数据目录".to_string())?
            .join("H3 Production Studio")
            .join("Projects")
    };
    fs::create_dir_all(&projects).map_err(|error| format!("无法创建应用项目库：{error}"))?;
    Ok(projects.to_string_lossy().to_string())
}

#[tauri::command]
fn save_project(project_path: String, project: Value) -> Result<(), String> {
    let path = PathBuf::from(project_path);
    atomic_json_write(&path, &project)
}

#[tauri::command]
fn load_project(project_path: String) -> Result<Value, String> {
    let path = PathBuf::from(project_path);
    if !path.exists() {
        return Err("项目文件尚未从 iCloud 下载，或已被移动".to_string());
    }
    let metadata = fs::metadata(&path).map_err(|error| error.to_string())?;
    if metadata.len() == 0 {
        return Err("项目文件为空；请检查 iCloud 同步状态".to_string());
    }
    let raw = fs::read_to_string(&path).map_err(|error| error.to_string())?;
    let mut project: Value =
        serde_json::from_str(&raw).map_err(|error| format!("项目 JSON 无法读取：{error}"))?;
    if project.get("schemaVersion").and_then(Value::as_u64) != Some(1) {
        return Err("项目版本不受支持；原文件没有被修改".to_string());
    }
    if let Some(assets) = project.get_mut("assets").and_then(Value::as_array_mut) {
        let root = path.parent().unwrap_or_else(|| Path::new("."));
        for asset in assets {
            if let Some(relative) = asset.get("relativePath").and_then(Value::as_str) {
                let preview = root.join(relative);
                if preview.is_file() {
                    asset["previewUrl"] = Value::String(preview.to_string_lossy().to_string());
                }
            }
        }
    }
    Ok(project)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ImportedAsset {
    id: String,
    name: String,
    kind: String,
    relative_path: String,
    bytes: u64,
    sha256: String,
    preview_url: String,
}

fn asset_kind(path: &Path) -> Option<&'static str> {
    match path
        .extension()?
        .to_string_lossy()
        .to_ascii_lowercase()
        .as_str()
    {
        "png" | "jpg" | "jpeg" | "webp" | "heic" => Some("image"),
        "mp4" | "mov" | "m4v" | "webm" => Some("video"),
        "wav" | "mp3" | "m4a" | "aac" | "flac" => Some("audio"),
        _ => None,
    }
}

#[tauri::command]
fn import_assets(project_path: String, sources: Vec<String>) -> Result<Vec<ImportedAsset>, String> {
    let root = project_root(&project_path)?;
    let assets_root = root.join("Assets");
    fs::create_dir_all(&assets_root).map_err(|error| error.to_string())?;
    let mut imported = Vec::new();
    for source in sources.into_iter().take(32) {
        let source_path = PathBuf::from(source);
        if !source_path.is_file() {
            return Err(format!(
                "素材不存在或尚未从 iCloud 下载：{}",
                source_path.display()
            ));
        }
        let kind = asset_kind(&source_path)
            .ok_or_else(|| format!("不支持的素材格式：{}", source_path.display()))?;
        let bytes = fs::read(&source_path).map_err(|error| error.to_string())?;
        let sha256 = hex::encode(Sha256::digest(&bytes));
        let source_name = source_path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy();
        let prefix = &sha256[..10];
        let destination_name = format!("{prefix}_{source_name}");
        let destination = assets_root.join(&destination_name);
        if !destination.exists() {
            fs::write(&destination, &bytes).map_err(|error| error.to_string())?;
        }
        imported.push(ImportedAsset {
            id: Uuid::new_v4().to_string(),
            name: source_name.to_string(),
            kind: kind.to_string(),
            relative_path: format!("Assets/{destination_name}"),
            bytes: bytes.len() as u64,
            sha256,
            preview_url: destination.to_string_lossy().to_string(),
        });
    }
    Ok(imported)
}

#[tauri::command]
fn write_job(project_path: String, job: Value) -> Result<(), String> {
    let root = project_root(&project_path)?;
    let id = job
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| "任务缺少 ID".to_string())?;
    let job_dir = root.join("Jobs").join(id);
    fs::create_dir_all(&job_dir).map_err(|error| error.to_string())?;
    atomic_json_write(&job_dir.join("job.json"), &job)
}

#[tauri::command]
fn load_jobs(project_path: String) -> Result<Vec<Value>, String> {
    let root = project_root(&project_path)?.join("Jobs");
    if !root.exists() {
        return Ok(Vec::new());
    }
    let mut jobs = Vec::new();
    for entry in fs::read_dir(root).map_err(|error| error.to_string())? {
        let path = entry
            .map_err(|error| error.to_string())?
            .path()
            .join("job.json");
        if !path.is_file() {
            continue;
        }
        let raw = fs::read_to_string(path).map_err(|error| error.to_string())?;
        let job: Value = serde_json::from_str(&raw).map_err(|error| error.to_string())?;
        jobs.push(job);
    }
    jobs.sort_by_key(|job| {
        job.get("createdAt")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    });
    Ok(jobs)
}

#[tauri::command]
fn set_secret(account: String, secret: String) -> Result<(), String> {
    if secret.trim().is_empty() {
        return Err("密钥不能为空".to_string());
    }
    entry(&account)?
        .set_password(&secret)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn get_secret(account: String) -> Result<String, String> {
    entry(&account)?
        .get_password()
        .map_err(|_| "钥匙串中没有找到所需密钥".to_string())
}

#[tauri::command]
fn has_secret(account: String) -> bool {
    entry(&account)
        .and_then(|item| item.get_password().map_err(|error| error.to_string()))
        .map(|secret| !secret.trim().is_empty())
        .unwrap_or(false)
}

#[tauri::command]
fn delete_secret(account: String) -> Result<(), String> {
    entry(&account)?
        .delete_credential()
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn write_job_diagnostic(
    project_path: String,
    job_id: String,
    diagnostic: Value,
) -> Result<(), String> {
    let root = project_root(&project_path)?;
    let job_dir = root.join("Jobs").join(safe_file_component(&job_id));
    fs::create_dir_all(&job_dir).map_err(|error| error.to_string())?;
    atomic_json_write(&job_dir.join("diagnostic.json"), &diagnostic)
}

fn runpod_key() -> Result<String, String> {
    get_secret("runpod-api-key".to_string())
}

fn runpod_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .map_err(|error| error.to_string())
}

async fn runpod_error(response: reqwest::Response) -> String {
    let status = response.status();
    let body = response.text().await.unwrap_or_default();
    format!("RunPod 请求失败（{status}）：{body}")
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Quote {
    gpu: String,
    available: bool,
    hourly_rate_usd: f64,
    source: String,
    data_center_id: Option<String>,
    data_center_location: Option<String>,
    stock_status: Option<String>,
    alternatives: Vec<QuoteAlternative>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct QuoteAlternative {
    gpu: String,
    available: bool,
    hourly_rate_usd: f64,
    data_center_id: Option<String>,
}

fn value_f64(value: Option<&Value>) -> Option<f64> {
    value.and_then(|item| item.as_f64().or_else(|| item.as_str()?.parse().ok()))
}

async fn runpod_graphql(key: &str, query: &str, variables: Value) -> Result<Value, String> {
    let response = runpod_client()?
        .post(RUNPOD_GRAPHQL)
        .query(&[("api_key", key)])
        .json(&json!({ "query": query, "variables": variables }))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    let body: Value = response.json().await.map_err(|error| error.to_string())?;
    if let Some(errors) = body.get("errors") {
        return Err(format!("RunPod GraphQL 请求失败：{errors}"));
    }
    Ok(body)
}

fn stock_available(status: &str) -> bool {
    !matches!(
        status.trim().to_ascii_lowercase().as_str(),
        "" | "none" | "unavailable" | "out of stock" | "no stock"
    )
}

fn allowed_data_center_for(data: &Value, gpu_name: &str) -> Option<(String, String, String)> {
    let centers = data.get("data")?.get("dataCenters")?.as_array()?;
    for allowed in ALLOWED_DATA_CENTERS {
        let Some(center) = centers
            .iter()
            .find(|item| item.get("id").and_then(Value::as_str) == Some(allowed))
        else {
            continue;
        };
        let gpu = center
            .get("gpuAvailability")
            .and_then(Value::as_array)
            .and_then(|items| {
                items
                    .iter()
                    .find(|item| item.get("gpuTypeId").and_then(Value::as_str) == Some(gpu_name))
            });
        let status = gpu
            .and_then(|item| item.get("stockStatus"))
            .and_then(Value::as_str)
            .unwrap_or("none");
        if stock_available(status) {
            return Some((
                allowed.to_string(),
                center
                    .get("location")
                    .and_then(Value::as_str)
                    .unwrap_or(allowed)
                    .to_string(),
                status.to_string(),
            ));
        }
    }
    None
}

const GPU_QUOTE_QUERY: &str = r#"query H3GpuQuote {
  gpuTypes {
    id
    displayName
    securePrice
    lowestPrice(input: { gpuCount: 1, secureCloud: true }) {
      stockStatus
      uninterruptablePrice
      availableGpuCounts
    }
  }
  dataCenters {
    id
    name
    location
    gpuAvailability { gpuTypeId displayName stockStatus }
  }
}"#;

#[tauri::command]
async fn runpod_quote(gpu_name: String) -> Result<Quote, String> {
    let key = runpod_key()?;
    let data = runpod_graphql(&key, GPU_QUOTE_QUERY, json!({})).await?;
    let list = data
        .get("data")
        .and_then(|value| value.get("gpuTypes"))
        .and_then(Value::as_array)
        .ok_or_else(|| "RunPod GPU 报价格式无法识别".to_string())?;
    let parse = |item: &Value| {
        let price = item.get("lowestPrice")?;
        let gpu = item.get("id").and_then(Value::as_str).unwrap_or_default();
        let center = allowed_data_center_for(&data, gpu);
        Some(QuoteAlternative {
            gpu: gpu.to_string(),
            available: center.is_some(),
            hourly_rate_usd: value_f64(price.get("uninterruptablePrice"))
                .or_else(|| value_f64(item.get("securePrice")))
                .unwrap_or(0.0),
            data_center_id: center.map(|value| value.0),
        })
    };
    let preferred_item = list
        .iter()
        .find(|item| item.get("id").and_then(Value::as_str) == Some(gpu_name.as_str()))
        .ok_or_else(|| format!("RunPod 当前没有返回 {gpu_name}"))?;
    let preferred =
        parse(preferred_item).ok_or_else(|| "RunPod 没有返回首选 GPU 的价格".to_string())?;
    let preferred_center = allowed_data_center_for(&data, &gpu_name);
    let alternatives = ["NVIDIA H100 80GB HBM3", "NVIDIA H200"]
        .iter()
        .filter(|gpu_id| **gpu_id != gpu_name)
        .filter_map(|gpu_id| {
            list.iter()
                .find(|item| item.get("id").and_then(Value::as_str) == Some(*gpu_id))
        })
        .filter_map(parse)
        .collect();
    Ok(Quote {
        gpu: gpu_name,
        available: preferred.available,
        hourly_rate_usd: preferred.hourly_rate_usd,
        source: "live".to_string(),
        data_center_id: preferred_center.as_ref().map(|value| value.0.clone()),
        data_center_location: preferred_center.as_ref().map(|value| value.1.clone()),
        stock_status: preferred_center.as_ref().map(|value| value.2.clone()),
        alternatives,
    })
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ConnectionTest {
    connected: bool,
    client_balance: f64,
    current_spend_per_hour: f64,
    active_pods: usize,
    allowed_regions_with_stock: usize,
}

#[tauri::command]
async fn runpod_test_connection() -> Result<ConnectionTest, String> {
    let key = runpod_key()?;
    let account = runpod_graphql(
        &key,
        r#"query H3ConnectionTest {
          myself { clientBalance currentSpendPerHr }
          dataCenters { id gpuAvailability { gpuTypeId stockStatus } }
        }"#,
        json!({}),
    )
    .await?;
    let response = runpod_client()?
        .get(format!("{RUNPOD_API}/pods"))
        .header(AUTHORIZATION, format!("Bearer {key}"))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    let pods: Value = response.json().await.map_err(|error| error.to_string())?;
    let myself = account.get("data").and_then(|value| value.get("myself"));
    let allowed_regions_with_stock = ALLOWED_DATA_CENTERS
        .iter()
        .filter(|id| {
            account
                .get("data")
                .and_then(|value| value.get("dataCenters"))
                .and_then(Value::as_array)
                .and_then(|centers| {
                    centers
                        .iter()
                        .find(|center| center.get("id").and_then(Value::as_str) == Some(*id))
                })
                .and_then(|center| center.get("gpuAvailability"))
                .and_then(Value::as_array)
                .map(|gpus| {
                    gpus.iter().any(|gpu| {
                        gpu.get("stockStatus")
                            .and_then(Value::as_str)
                            .map(stock_available)
                            .unwrap_or(false)
                    })
                })
                .unwrap_or(false)
        })
        .count();
    Ok(ConnectionTest {
        connected: true,
        client_balance: value_f64(myself.and_then(|value| value.get("clientBalance")))
            .unwrap_or(0.0),
        current_spend_per_hour: value_f64(myself.and_then(|value| value.get("currentSpendPerHr")))
            .unwrap_or(0.0),
        active_pods: pods.as_array().map(Vec::len).unwrap_or(0),
        allowed_regions_with_stock,
    })
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ProjectSettings {
    preferred_gpu: String,
    runpod_image: String,
    runpod_proxy_port: u16,
    job_timeout_minutes: u32,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Session {
    id: String,
    status: String,
    gpu: String,
    hourly_rate_usd: f64,
    started_at: String,
    gateway_url: String,
    data_center_id: String,
    data_center_location: String,
    hard_terminate_at: String,
}

fn normalized_profiles(profiles: &[String]) -> Vec<String> {
    let mut selected: Vec<String> = profiles
        .iter()
        .map(|profile| profile.trim().to_ascii_lowercase())
        .filter(|profile| matches!(profile.as_str(), "ref2va" | "fl2va" | "seedvr2"))
        .collect();
    selected.sort();
    selected.dedup();
    if !selected
        .iter()
        .any(|profile| matches!(profile.as_str(), "ref2va" | "fl2va"))
    {
        selected.push("ref2va".to_string());
    }
    selected
}

fn build_pod_variables(
    settings: &ProjectSettings,
    gateway_token: &str,
    workload_profiles: &[String],
    data_center_id: &str,
    hard_terminate_at: &str,
) -> Value {
    let profiles = normalized_profiles(workload_profiles);
    json!({
        "name": format!("H3-Production-{}", Utc::now().format("%m%d-%H%M")),
        "cloudType": "SECURE",
        "gpuCount": 1,
        "gpuTypeId": settings.preferred_gpu,
        "imageName": settings.runpod_image,
        "containerDiskInGb": 120,
        // GraphQL 用 0 表示不创建 Pod 卷；REST 接口中的等价值为 null。
        "volumeInGb": 0,
        "ports": format!("{}/http", settings.runpod_proxy_port),
        "dataCenterId": data_center_id,
        "allowedCudaVersions": ["13.0"],
        "terminateAfter": hard_terminate_at,
        "env": [
            { "key": "H3_GATEWAY_TOKEN", "value": gateway_token },
            { "key": "H3_IDLE_OWNER", "value": "mac-controller" },
            { "key": "H3_MODEL_PROFILE", "value": profiles.join(",") },
            { "key": "H3_JOB_TIMEOUT_MIN", "value": settings.job_timeout_minutes.to_string() }
        ]
    })
}

async fn terminate_pod_with_key(key: &str, pod_id: &str) -> Result<(), String> {
    let response = runpod_client()?
        .delete(format!("{RUNPOD_API}/pods/{pod_id}"))
        .header(AUTHORIZATION, format!("Bearer {key}"))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() && response.status().as_u16() != 404 {
        return Err(runpod_error(response).await);
    }
    Ok(())
}

fn pod_location(pod: &Value) -> Option<(String, String)> {
    let machine = pod.get("machine")?;
    Some((
        machine.get("dataCenterId")?.as_str()?.to_string(),
        machine
            .get("location")
            .and_then(Value::as_str)
            .unwrap_or("未知地区")
            .to_string(),
    ))
}

#[tauri::command]
async fn runpod_create_session(
    settings: ProjectSettings,
    workload_profiles: Vec<String>,
    data_center_id: String,
) -> Result<Session, String> {
    if !settings.runpod_image.starts_with("ghcr.io/")
        || !settings.runpod_image.contains("@sha256:")
        || settings.runpod_image.ends_with(":latest")
    {
        return Err("应用内置云端镜像尚未锁定到有效的 GHCR SHA256 摘要".to_string());
    }
    if !ALLOWED_DATA_CENTERS.contains(&data_center_id.as_str()) {
        return Err("所选数据中心不在 MiniMax H3 许可地区名单中".to_string());
    }
    let key = runpod_key()?;
    let gateway_token = Uuid::new_v4().to_string();
    let hard_terminate_at = (Utc::now() + Duration::hours(SESSION_HARD_LIMIT_HOURS)).to_rfc3339();
    let variables = build_pod_variables(
        &settings,
        &gateway_token,
        &workload_profiles,
        &data_center_id,
        &hard_terminate_at,
    );
    let created = runpod_graphql(
        &key,
        r#"mutation H3Create(
          $name: String, $cloudType: CloudTypeEnum, $gpuCount: Int,
          $gpuTypeId: String, $imageName: String, $containerDiskInGb: Int,
          $volumeInGb: Int, $ports: String, $env: [EnvironmentVariableInput],
          $dataCenterId: String, $allowedCudaVersions: [String], $terminateAfter: DateTime
        ) {
          podFindAndDeployOnDemand(input: {
            name: $name, cloudType: $cloudType, gpuCount: $gpuCount,
            gpuTypeId: $gpuTypeId, imageName: $imageName,
            containerDiskInGb: $containerDiskInGb, volumeInGb: $volumeInGb,
            ports: $ports, env: $env, dataCenterId: $dataCenterId,
            allowedCudaVersions: $allowedCudaVersions, terminateAfter: $terminateAfter
          }) { id }
        }"#,
        variables,
    )
    .await?;
    let id = created
        .get("data")
        .and_then(|value| value.get("podFindAndDeployOnDemand"))
        .and_then(|value| value.get("id"))
        .and_then(Value::as_str)
        .ok_or_else(|| "RunPod 没有返回 Pod ID".to_string())?
        .to_string();
    let response = runpod_client()?
        .get(format!("{RUNPOD_API}/pods/{id}"))
        .header(AUTHORIZATION, format!("Bearer {key}"))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        let reason = runpod_error(response).await;
        let _ = terminate_pod_with_key(&key, &id).await;
        return Err(format!(
            "Pod 已创建但无法核对实际地区，已自动删除：{reason}"
        ));
    }
    let pod: Value = response.json().await.map_err(|error| error.to_string())?;
    if pod.get("volumeInGb").and_then(Value::as_u64).unwrap_or(0) > 0
        || pod
            .get("networkVolume")
            .is_some_and(|value| !value.is_null())
    {
        let _ = terminate_pod_with_key(&key, &id).await;
        return Err("RunPod 意外创建了付费卷，已立即删除 Pod".to_string());
    }
    let (actual_data_center, actual_location) = match pod_location(&pod) {
        Some(location) => location,
        None => {
            let _ = terminate_pod_with_key(&key, &id).await;
            return Err("Pod 已创建但 RunPod 未返回实际数据中心，已自动删除".to_string());
        }
    };
    if actual_data_center != data_center_id
        || !ALLOWED_DATA_CENTERS.contains(&actual_data_center.as_str())
    {
        let _ = terminate_pod_with_key(&key, &id).await;
        return Err(format!(
            "RunPod 实际分配到 {actual_data_center}，与确认的 {data_center_id} 不符，已自动删除且未上传素材"
        ));
    }
    let rate = value_f64(pod.get("costPerHr"))
        .or_else(|| value_f64(pod.get("adjustedCostPerHr")))
        .or_else(|| value_f64(pod.get("machine").and_then(|value| value.get("costPerHr"))))
        .unwrap_or(0.0);
    set_secret(format!("gateway:{id}"), gateway_token)?;
    Ok(Session {
        gateway_url: format!(
            "https://{}-{}.proxy.runpod.net",
            id, settings.runpod_proxy_port
        ),
        id,
        status: "starting".to_string(),
        gpu: settings.preferred_gpu,
        hourly_rate_usd: rate,
        started_at: Utc::now().to_rfc3339(),
        data_center_id: actual_data_center,
        data_center_location: actual_location,
        hard_terminate_at,
    })
}

#[tauri::command]
async fn runpod_recover_session(
    pod_id: String,
    gpu: String,
    proxy_port: u16,
) -> Result<Session, String> {
    let key = runpod_key()?;
    let response = runpod_client()?
        .get(format!("{RUNPOD_API}/pods/{pod_id}"))
        .header(AUTHORIZATION, format!("Bearer {key}"))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    let pod: Value = response.json().await.map_err(|error| error.to_string())?;
    let desired = pod
        .get("desiredStatus")
        .or_else(|| pod.get("status"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if matches!(desired, "TERMINATED" | "EXITED" | "DELETED") {
        return Err("原 GPU 会话已经结束".to_string());
    }
    let rate = value_f64(pod.get("costPerHr"))
        .or_else(|| value_f64(pod.get("adjustedCostPerHr")))
        .unwrap_or(0.0);
    let (data_center_id, data_center_location) = match pod_location(&pod) {
        Some(location) if ALLOWED_DATA_CENTERS.contains(&location.0.as_str()) => location,
        Some(location) => {
            terminate_pod_with_key(&key, &pod_id).await?;
            return Err(format!(
                "恢复时发现 Pod 位于不允许的地区 {}，已自动删除",
                location.0
            ));
        }
        None => {
            terminate_pod_with_key(&key, &pod_id).await?;
            return Err("恢复时无法核对 Pod 地区，已自动删除".to_string());
        }
    };
    let started_at = pod.get("createdAt").and_then(Value::as_str).unwrap_or("");
    let recovered_started = chrono::DateTime::parse_from_rfc3339(started_at)
        .map(|value| value.with_timezone(&Utc))
        .unwrap_or_else(|_| Utc::now());
    Ok(Session {
        gateway_url: format!("https://{}-{}.proxy.runpod.net", pod_id, proxy_port),
        id: pod_id,
        status: "starting".to_string(),
        gpu,
        hourly_rate_usd: rate,
        started_at: recovered_started.to_rfc3339(),
        data_center_id,
        data_center_location,
        hard_terminate_at: (recovered_started + Duration::hours(SESSION_HARD_LIMIT_HOURS))
            .to_rfc3339(),
    })
}

#[tauri::command]
async fn runpod_terminate_session(pod_id: String) -> Result<(), String> {
    let key = runpod_key()?;
    terminate_pod_with_key(&key, &pod_id).await?;
    let _ = delete_secret(format!("gateway:{pod_id}"));
    Ok(())
}

async fn serverless_call(
    endpoint_id: &str,
    suffix: &str,
    method: reqwest::Method,
    body: Option<Value>,
) -> Result<Value, String> {
    let key = runpod_key()?;
    let url = format!("https://api.runpod.ai/v2/{endpoint_id}/{suffix}");
    let mut request = runpod_client()?.request(method, url).bearer_auth(key);
    if let Some(payload) = body {
        request = request.json(&payload);
    }
    let response = request.send().await.map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    response.json().await.map_err(|error| error.to_string())
}

#[tauri::command]
async fn serverless_run(endpoint_id: String, input: Value) -> Result<Value, String> {
    serverless_call(
        &endpoint_id,
        "run",
        reqwest::Method::POST,
        Some(json!({"input": input})),
    )
    .await
}

#[tauri::command]
async fn serverless_status(endpoint_id: String, job_id: String) -> Result<Value, String> {
    serverless_call(
        &endpoint_id,
        &format!("status/{job_id}"),
        reqwest::Method::GET,
        None,
    )
    .await
}

#[tauri::command]
async fn serverless_cancel(endpoint_id: String, job_id: String) -> Result<Value, String> {
    serverless_call(
        &endpoint_id,
        &format!("cancel/{job_id}"),
        reqwest::Method::POST,
        None,
    )
    .await
}

#[tauri::command]
async fn upload_project_asset(
    url: String,
    token: String,
    project_path: String,
    relative_path: String,
) -> Result<Value, String> {
    let root = project_root(&project_path)?;
    let source = root.join(&relative_path);
    let canonical_root = root.canonicalize().map_err(|error| error.to_string())?;
    let canonical_source = source.canonicalize().map_err(|error| error.to_string())?;
    if !canonical_source.starts_with(&canonical_root) {
        return Err("素材路径超出了项目目录".to_string());
    }
    let name = source
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();
    let bytes = tokio::fs::read(&source)
        .await
        .map_err(|error| error.to_string())?;
    let part = reqwest::multipart::Part::bytes(bytes).file_name(name);
    let form = reqwest::multipart::Form::new().part("file", part);
    let response = runpod_client()?
        .post(url)
        .bearer_auth(token)
        .multipart(form)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    response.json().await.map_err(|error| error.to_string())
}

#[tauri::command]
async fn download_artifact(
    url: String,
    token: String,
    project_path: String,
    job_id: String,
) -> Result<String, String> {
    let response = runpod_client()?
        .get(url)
        .bearer_auth(token)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    let bytes = response.bytes().await.map_err(|error| error.to_string())?;
    if bytes.len() < 1024 {
        return Err("云端返回的成片文件异常小，未写入项目".to_string());
    }
    let root = project_root(&project_path)?;
    let outputs = root.join("Outputs");
    fs::create_dir_all(&outputs).map_err(|error| error.to_string())?;
    let destination = outputs.join(format!(
        "{}_{}.mp4",
        Utc::now().format("%Y%m%d_%H%M%S"),
        safe_file_component(&job_id)
    ));
    fs::write(&destination, &bytes).map_err(|error| error.to_string())?;
    let probe = Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
        ])
        .arg(&destination)
        .output();
    match probe {
        Ok(result) if result.status.success() => {
            let report = String::from_utf8_lossy(&result.stdout);
            let parsed: Value = serde_json::from_str(&report).map_err(|error| error.to_string())?;
            let duration = parsed
                .get("format")
                .and_then(|value| value.get("duration"))
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<f64>().ok())
                .unwrap_or(0.0);
            if duration < 0.5 {
                let _ = fs::remove_file(&destination);
                return Err("成片媒体校验失败：没有有效时长".to_string());
            }
            let verification = json!({
                "sha256": hex::encode(Sha256::digest(&bytes)),
                "bytes": bytes.len(),
                "ffprobe": parsed,
                "verifiedAt": Utc::now().to_rfc3339()
            });
            atomic_json_write(
                &destination.with_extension("verification.json"),
                &verification,
            )?;
        }
        Ok(result) => {
            let _ = fs::remove_file(&destination);
            return Err(format!(
                "成片媒体校验失败：{}",
                String::from_utf8_lossy(&result.stderr)
            ));
        }
        Err(error) => {
            let _ = fs::remove_file(&destination);
            return Err(format!("无法运行 ffprobe 校验成片：{error}"));
        }
    }
    Ok(destination.to_string_lossy().to_string())
}

#[tauri::command]
async fn download_public_artifact(
    url: String,
    project_path: String,
    job_id: String,
) -> Result<String, String> {
    if !url.starts_with("https://") {
        return Err("Serverless 成片地址必须使用 HTTPS".to_string());
    }
    let response = runpod_client()?
        .get(&url)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    if !response.status().is_success() {
        return Err(runpod_error(response).await);
    }
    let bytes = response.bytes().await.map_err(|error| error.to_string())?;
    let root = project_root(&project_path)?;
    let outputs = root.join("Outputs");
    fs::create_dir_all(&outputs).map_err(|error| error.to_string())?;
    let destination = outputs.join(format!(
        "{}_{}.mp4",
        Utc::now().format("%Y%m%d_%H%M%S"),
        safe_file_component(&job_id)
    ));
    fs::write(&destination, &bytes).map_err(|error| error.to_string())?;
    let probe = Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
        ])
        .arg(&destination)
        .output()
        .map_err(|error| error.to_string())?;
    if !probe.status.success() {
        let _ = fs::remove_file(&destination);
        return Err("Serverless 成片没有通过媒体校验".to_string());
    }
    Ok(destination.to_string_lossy().to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            default_project_parent,
            create_project,
            save_project,
            load_project,
            import_assets,
            write_job,
            load_jobs,
            set_secret,
            get_secret,
            has_secret,
            delete_secret,
            write_job_diagnostic,
            runpod_quote,
            runpod_test_connection,
            runpod_create_session,
            runpod_recover_session,
            runpod_terminate_session,
            serverless_run,
            serverless_status,
            serverless_cancel,
            upload_project_asset,
            download_artifact,
            download_public_artifact,
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                #[cfg(target_os = "macos")]
                window.set_decorations(true)?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("无法启动 H3 Production Studio");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_name_removes_path_characters() {
        assert_eq!(safe_project_name("广告/第一版:测试"), "广告 第一版 测试");
        assert_eq!(safe_project_name("   "), "H3 项目");
    }

    #[test]
    fn atomic_write_keeps_a_backup() {
        let root = std::env::temp_dir().join(format!("h3-studio-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("project.h3.json");
        atomic_json_write(&path, &json!({"schemaVersion": 1, "name": "初稿"})).unwrap();
        atomic_json_write(&path, &json!({"schemaVersion": 1, "name": "新版"})).unwrap();
        let backup = fs::read_to_string(path.with_extension("json.h3studio-backup")).unwrap();
        assert!(backup.contains("初稿"));
        let current = fs::read_to_string(&path).unwrap();
        assert!(current.contains("新版"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn pod_request_has_no_persistent_volume_and_hard_cost_guard() {
        let settings = ProjectSettings {
            preferred_gpu: "NVIDIA RTX PRO 6000 Blackwell Server Edition".to_string(),
            runpod_image: "example/h3@sha256:abc".to_string(),
            runpod_proxy_port: 8000,
            job_timeout_minutes: 45,
        };
        let request = build_pod_variables(
            &settings,
            "one-time-token",
            &["ref2va".to_string(), "seedvr2".to_string()],
            "OC-AU-1",
            "2030-01-01T03:00:00Z",
        );
        assert_eq!(request["volumeInGb"], 0);
        assert_eq!(request["ports"], "8000/http");
        assert_eq!(request["dataCenterId"], "OC-AU-1");
        assert_eq!(request["allowedCudaVersions"][0], "13.0");
        assert_eq!(request["terminateAfter"], "2030-01-01T03:00:00Z");
        let env = request["env"].as_array().unwrap();
        assert!(env
            .iter()
            .any(|item| item["key"] == "H3_GATEWAY_TOKEN" && item["value"] == "one-time-token"));
        assert!(env
            .iter()
            .any(|item| item["key"] == "H3_MODEL_PROFILE" && item["value"] == "ref2va,seedvr2"));
    }

    #[test]
    fn data_center_selection_follows_license_safe_priority() {
        let response = json!({
            "data": { "dataCenters": [
                { "id": "AP-JP-1", "location": "Japan", "gpuAvailability": [
                    { "gpuTypeId": "GPU", "stockStatus": "High" }
                ]},
                { "id": "OC-AU-1", "location": "Australia", "gpuAvailability": [
                    { "gpuTypeId": "GPU", "stockStatus": "Low" }
                ]},
                { "id": "US-GA-1", "location": "United States", "gpuAvailability": [
                    { "gpuTypeId": "GPU", "stockStatus": "High" }
                ]}
            ]}
        });
        let selected = allowed_data_center_for(&response, "GPU").unwrap();
        assert_eq!(selected.0, "OC-AU-1");
    }

    #[test]
    fn output_filename_component_cannot_escape_outputs_directory() {
        assert_eq!(safe_file_component("../../job-123"), "job-123");
    }
}
