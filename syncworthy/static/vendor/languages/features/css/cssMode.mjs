// Deliberately empty.  The stock build of this mode is a separate bundle that carries
// its own copy of the whole editor (a second Monaco) and needs a language-service
// worker; Syncworthy is a text editor first, so these language services are off.
// Highlighting comes from the Monarch grammars in ../../definitions/ and js/modules/languages.js.
const none = () => ({ dispose() {} });
export const setupMode = none, setupTypeScript = none, setupJavaScript = none;
export const getWorker = () => Promise.reject(new Error('language services are not shipped'));
export const getTypeScriptWorker = getWorker, getJavaScriptWorker = getWorker;
