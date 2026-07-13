/**
 * GitHub Pages 播放页的最小交互层。
 *
 * 视频始终从 v0.1.0 的 GitHub Release 读取，不进入 Pages 构建产物，避免把大文件提交到源码。
 * Release 尚未创建或资产不可用时保留首帧海报，并将状态文本改为可理解的提示。
 */

const videoUrl = "https://github.com/breakstring/agent-deck/releases/download/v0.1.0/agent-deck-intro.mp4";
const video = document.querySelector("#product-video");
const playerShell = document.querySelector("[data-player-shell]");
const videoStatus = document.querySelector("#video-status");

/** 更新播放页的无障碍状态提示。 */
function setVideoStatus(message, unavailable = false) {
  videoStatus.textContent = message;
  playerShell.classList.toggle("is-unavailable", unavailable);
}

/** 为播放器绑定 Release 资产，并在可用性变化时更新提示。 */
function initializeVideo() {
  video.src = videoUrl;

  video.addEventListener("loadedmetadata", () => {
    setVideoStatus("视频已就绪 · 约 1 分 15 秒 · 1080p");
  });

  video.addEventListener("playing", () => {
    setVideoStatus("正在播放产品介绍视频。");
  });

  video.addEventListener("pause", () => {
    if (!video.ended && video.readyState >= HTMLMediaElement.HAVE_METADATA) {
      setVideoStatus("已暂停。可继续播放，或下载 1080p MP4。 ");
    }
  });

  video.addEventListener("ended", () => {
    setVideoStatus("播放完成。欢迎在 GitHub 查看项目源码。 ");
  });

  video.addEventListener("error", () => {
    setVideoStatus("正式视频正在准备中。首个 v0.1.0 Release 发布后，此处会自动可播放。", true);
  });
}

initializeVideo();
