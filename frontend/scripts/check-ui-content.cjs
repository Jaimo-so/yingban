const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {runInNewContext} = require('node:vm');

const web = resolve(__dirname, '../../web');
const context = {window: {}};
runInNewContext(readFileSync(resolve(web, 'ui-content.js'), 'utf8'), context);
context.window.YingbanContent.validate(JSON.parse(readFileSync(resolve(web, 'ui-content.json'), 'utf8')));
console.log('ui-content.json: editable content, fields, actions and placeholders are valid.');
