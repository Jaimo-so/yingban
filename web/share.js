async function loadShare() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || "";
  const root = document.querySelector("#public-share-card");
  const loading = document.querySelector("#public-share-loading");
  const showError = (title, message) => {
    window.YingbanShareCard.render({ content: {
      title,
      text: message,
      attribution: "分享可能已过期或被创建者撤回。",
    } }, root);
    loading.hidden = true;
    root.hidden = false;
  };
  if (!token) {
    showError("这张分享链接不完整", "请向分享者索取完整链接。");
    return;
  }
  try {
    const response = await fetch(`/api/public/share-cards/${encodeURIComponent(token)}`, {
      credentials: "omit",
      headers: { "Accept": "application/json" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "分享暂时无法打开");
    window.YingbanShareCard.render(data.card, root);
    document.title = `${data.card.content.title || "电影分享"} · 影伴`;
    loading.hidden = true;
    root.hidden = false;
  } catch (error) {
    showError("这张分享已不可用", error.message);
  }
}

loadShare();
