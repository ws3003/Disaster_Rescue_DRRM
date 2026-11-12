// 微信 Canvas 2D 绘制；只连接相邻且都有效的告警快照。
function drawTemperatureTrend(ctx, model, width, height) {
  ctx.clearRect(0, 0, width, height);
  const points = model.points || [];
  const values = points.filter(function (p) { return p.value !== null; }).map(function (p) { return p.value; });
  if (!values.length || width < 100 || height < 80) return;

  const left = 52, right = 22, top = 35, bottom = height - 32;
  const lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
  const padding = Math.max(1, (hi - lo) * 0.25);
  const min = Math.floor((lo - padding) * 10) / 10;
  const max = Math.ceil((hi + padding) * 10) / 10;
  const plotWidth = width - left - right;
  const x = function (index) { return left + (points.length === 1 ? plotWidth / 2 : index / (points.length - 1) * plotWidth); };
  const y = function (value) { return bottom - (value - min) / (max - min) * (bottom - top); };

  ctx.font = '14px sans-serif';
  ctx.fillStyle = '#334b60';
  ctx.textAlign = 'left';
  ctx.fillText('℃', 5, 16);
  [max, (max + min) / 2, min].forEach(function (value) {
    const yy = y(value);
    ctx.beginPath();
    ctx.strokeStyle = '#d9e4ed';
    ctx.lineWidth = 1;
    ctx.moveTo(left, yy);
    ctx.lineTo(width - right, yy);
    ctx.stroke();
    ctx.fillStyle = '#334b60';
    ctx.textAlign = 'right';
    ctx.fillText(value.toFixed(1), left - 9, yy + 5);
  });

  points.forEach(function (point, index) {
    if (point.value === null || index === 0 || points[index - 1].value === null) return;
    ctx.beginPath();
    ctx.strokeStyle = '#0e7490';
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    ctx.moveTo(x(index - 1), y(points[index - 1].value));
    ctx.lineTo(x(index), y(point.value));
    ctx.stroke();
  });

  points.forEach(function (point, index) {
    ctx.fillStyle = '#334b60';
    ctx.textAlign = 'center';
    ctx.font = '14px sans-serif';
    ctx.fillText(String(point.ordinal), x(index), height - 8);
    if (point.value === null) return;
    const xx = x(index), yy = y(point.value);
    ctx.beginPath();
    ctx.arc(xx, yy, point.latest ? 6 : 4, 0, Math.PI * 2);
    ctx.fillStyle = point.latest ? '#1d4ed8' : '#0e7490';
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = '#10283e';
    ctx.font = 'bold 15px sans-serif';
    // 两端向内对齐，320px 屏幕也不裁掉温度文字。
    ctx.textAlign = points.length === 1 ? 'center' : index === 0 ? 'left' : index === points.length - 1 ? 'right' : 'center';
    ctx.fillText(String(point.value), xx, yy - 14);
  });
}

module.exports = { drawTemperatureTrend };
