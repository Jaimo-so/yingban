const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {runInNewContext} = require('node:vm');
const web = resolve(__dirname, '../../web');
const source = readFileSync(resolve(web, 'ui-content.js'), 'utf8');
const original = () => JSON.parse(readFileSync(resolve(web, 'ui-content.json'), 'utf8'));
function runtime(fetch) {
  const context = {window: {}, fetch};
  runInNewContext(source, context);
  return context.window.YingbanContent;
}

test('editable copy, optional fields and action order can change independently', () => {
  const value = original();
  value.home.title = '新的首页标题';
  value.cards.home.fields = ['title', 'rank'];
  value.cards.home.actions.reverse();
  value.cards.home.actions[0].label = '开始讨论';
  value.poster.showRegions = false;
  assert.equal(runtime().validate(value), value);
});

test('bad edits identify the exact field instead of silently changing actions', () => {
  for (const [change, pattern] of [
    [x => {delete x.search.loading;}, /search.loading/],
    [x => {x.cards.home.actions[0].id = 'delete_account';}, /cards.home.actions\[0\].id/],
    [x => {x.cards.home.actions.push(x.cards.home.actions[0]);}, /不能重复动作/],
    [x => {x.cards.home.actions[0].label = '';}, /按钮名称不能为空/],
    [x => {x.cards.home.fields = ['title','title'];}, /不能重复字段/],
    [x => {x.cards.home.rank = '{unknown}';}, /不支持占位符/],
    [x => {x.poster.showYear = 'false';}, /poster.showYear/],
    [x => {x.home.titel = 'typo';}, /home.titel/],
  ]) {
    const value = original(); change(value);
    assert.throws(() => runtime().validate(value), pattern);
  }
});

test('runtime fetch reads fresh JSON and exposes edited data', async () => {
  const value = original(); value.home.title = '刷新后生效';
  const content = runtime(async (url, options) => {
    assert.equal(url, '/ui-content.json'); assert.equal(options.cache, 'no-store');
    return {ok: true, json: async () => value};
  });
  await content.load();
  assert.equal(content.get('home.title'), '刷新后生效');
  assert.equal(content.format('聊天记录 {count}', {count: 3}), '聊天记录 3');
});

test('missing or malformed JSON yields a recoverable configuration error', async () => {
  await assert.rejects(runtime(async () => ({ok: false, status: 404})).load(), /加载失败（404）/);
  await assert.rejects(runtime(async () => ({ok: true, json: async () => {throw new SyntaxError();}})).load(), /引号和逗号/);
});
