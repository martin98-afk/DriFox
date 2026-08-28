
(function(){
  var out = [];
  function t(name, fn){ try { out.push(name+'='+JSON.stringify(fn())); } catch(e){ out.push(name+'=ERR:'+e.message); } }
  t('bodyExists', function(){ return !!document.body; });
  t('bodyClass', function(){ return document.body.className; });
  t('bsTop', function(){ return document.body.scrollTop; });
  t('bsH', function(){ return document.body.scrollHeight; });
  t('bsC', function(){ return document.body.clientHeight; });
  t('styleOverflowY', function(){ return document.body.style.overflowY; });
  t('setTop', function(){ document.body.scrollTop = 100; return document.body.scrollTop; });
  t('hasFn', function(){ return typeof _autoScrollStreamingBody; });
  t('callFn', function(){ _autoScrollStreamingBody(); return 'called'; });
  return out.join(' | ');
})()
