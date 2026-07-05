async function loadReport() {
  const response = await fetch("data/report.json", { cache: "no-store" });
  const report = await response.json();

  document.getElementById("run-time").innerText =
    `Started: ${report.started_at} | Finished: ${report.finished_at}`;

  document.getElementById("total-channels").innerText = report.total_channels;
  document.getElementById("working-channels").innerText = report.working_channels;
  document.getElementById("broken-channels").innerText = report.broken_channels;
  document.getElementById("success-rate").innerText = `${report.success_rate_percent}%`;
  document.getElementById("epg-channels").innerText = report.epg.channel_count;

  const groupStats = document.getElementById("group-stats");

  Object.entries(report.group_stats)
    .sort(([a], [b]) => a.localeCompare(b))
    .forEach(([group, stats]) => {
      const success = stats.total > 0
        ? ((stats.working / stats.total) * 100).toFixed(2)
        : "0.00";

      groupStats.insertAdjacentHTML("beforeend", `
        <tr>
          <td>${escapeHtml(group)}</td>
          <td>${stats.total}</td>
          <td>${stats.working}</td>
          <td>${stats.broken}</td>
          <td>${success}%</td>
        </tr>
      `);
    });

  const epgSources = document.getElementById("epg-sources");

  report.epg.sources.forEach((source) => {
    epgSources.insertAdjacentHTML("beforeend", `
      <tr>
        <td class="${source.ok ? "good" : "bad"}">${source.ok ? "OK" : "FAILED"}</td>
        <td><code>${escapeHtml(source.url)}</code></td>
        <td>${source.channels}</td>
        <td>${source.programmes}</td>
        <td><code>${escapeHtml(source.error || "")}</code></td>
      </tr>
    `);
  });

  const brokenList = document.getElementById("broken-list");

  report.broken.slice(0, 200).forEach((item) => {
    brokenList.insertAdjacentHTML("beforeend", `
      <tr>
        <td>${escapeHtml(item.name)}</td>
        <td>${escapeHtml(item.group_title || "Ungrouped")}</td>
        <td><code>${escapeHtml(item.error || "")}</code></td>
        <td><a href="${escapeAttribute(item.url)}">stream</a></td>
      </tr>
    `);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

loadReport().catch((error) => {
  document.body.insertAdjacentHTML(
    "beforeend",
    `<pre>Failed to load report: ${escapeHtml(error)}</pre>`
  );
});
