const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { buildHomeFocus, markLatestByCategory } = require('../utils/home-focus');
function item(key, category, severity) { return { alertKey: key, category: category, severityKey: severity }; }
test('首页仅选两条高严重告警，选中与展开区域分别保留服务端组内顺序', () => {
  const input = [item('p1','person','low'), item('p2','person','high'), item('s1','sensor_environment','high'), item('s2','sensor_environment','medium')];
  const before = JSON.stringify(input);
  const result = buildHomeFocus(input);
  assert.deepEqual(result.focus.map(x => x.alertKey), ['p2','s1']);
  assert.deepEqual(result.remaining.map(x => x.alertKey), ['p1','s2']);
  assert.equal(JSON.stringify(input), before);
});
test('首页空数据、单条和同等级告警边界', () => {
  assert.deepEqual(buildHomeFocus([]), { focus: [], remaining: [] });
  assert.equal(buildHomeFocus([item('p1','person',null)]).focus.length, 1);
  const result = buildHomeFocus(['1','2','3'].map(key => item(key,'person','high')));
  assert.deepEqual(result.focus.map(x => x.alertKey), ['1','2']);
  assert.equal(result.remaining.length, 1);
});
test('最新标记分别取各组首条；筛选掉最新项不会把旧项重新标最新', () => {
  const marked = markLatestByCategory([item('p1','person','low'),item('p2','person','high'),item('s1','sensor_environment','high')]);
  assert.deepEqual(marked.map(x => x.latestLabel), ['人员最新','','传感器最新']);
  assert.equal(marked.filter(x => x.severityKey === 'high')[0].latestLabel, '');
});
function component() {
  let definition, picker;
  const toasts=[];
  const wx = { chooseMedia(opts) { picker=opts; }, showToast(opts) { toasts.push(opts); } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../components/scene-media/index.js'),'utf8'), { Component(d) { definition=d; }, wx });
  const instance = { properties: { contextKey:'person:1' }, data: { imagePath:'',videoPath:'',choosing:false }, setData(patch) { Object.assign(this.data,patch); } };
  Object.keys(definition.methods).forEach(key => { instance[key]=definition.methods[key].bind(instance); });
  return { instance, definition, wx, getPicker: () => picker, toasts };
}
test('影像预览只用相册临时文件，不创建告警媒体 DTO；取消后可重试', () => {
  const ctx=component();
  ctx.instance.choosePreview({ currentTarget:{dataset:{kind:'image'}} });
  assert.deepEqual(Array.from(ctx.getPicker().sourceType),['album']);
  ctx.getPicker().success({tempFiles:[{tempFilePath:'wxfile://local.jpg'}]});
  ctx.getPicker().complete();
  assert.equal(ctx.instance.data.imagePath,'wxfile://local.jpg');
  assert.equal(ctx.instance.data.choosing,false);
  ctx.instance.choosePreview({currentTarget:{dataset:{kind:'video'}}});
  ctx.getPicker().fail({errMsg:'chooseMedia:fail cancel'});
  ctx.getPicker().complete();
  assert.equal(ctx.toasts.length,0);
});
test('切换告警清空本机预览，并忽略旧告警迟到的文件选择结果', () => {
  const ctx=component();
  ctx.instance.choosePreview({currentTarget:{dataset:{kind:'image'}}});
  const picker=ctx.getPicker();
  ctx.instance.properties.contextKey='sensor:2';
  ctx.definition.properties.contextKey.observer.call(ctx.instance);
  picker.success({tempFiles:[{tempFilePath:'wxfile://old.jpg'}]});
  assert.equal(ctx.instance.data.imagePath,'');
});

test('全屏操作定位当前组件播放器，进入全屏成功后继续播放，失败时保留内嵌播放', () => {
  const ctx=component();
  let requested=0, played=0, options;
  ctx.wx.createVideoContext=(id,scope) => {
    assert.equal(id,'sceneVideo');
    assert.equal(scope,ctx.instance);
    return { requestFullScreen(opts) { requested++; options=opts; }, play() { played++; } };
  };
  ctx.instance.expandVideo();
  assert.equal(requested,0);
  ctx.instance.data.videoPath='wxfile://scene.mp4';
  ctx.instance.expandVideo();
  assert.equal(requested,1);
  assert.equal(played,0);
  options.success();
  assert.equal(played,1);
  options.fail();
  assert.equal(ctx.instance.data.videoPath,'wxfile://scene.mp4');
  assert.equal(ctx.toasts.length,1);
});

test('图片点击预览当前本机照片；没有图片时不打开预览', () => {
  const ctx=component();
  const calls=[];
  ctx.wx.previewImage=opts => calls.push(opts);
  ctx.instance.previewImage();
  assert.equal(calls.length,0);
  ctx.instance.data.imagePath='wxfile://photo.jpg';
  ctx.instance.previewImage();
  assert.equal(calls.length,1);
  assert.equal(calls[0].current,'wxfile://photo.jpg');
  assert.deepEqual(Array.from(calls[0].urls),['wxfile://photo.jpg']);
});
