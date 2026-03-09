(function () {
  function injectPWA() {
    var head = document.head;

    // Web App Manifest
    var manifest = document.createElement('link');
    manifest.rel = 'manifest';
    manifest.href = '/public/manifest.json';
    head.appendChild(manifest);

    // Theme colour (Android Chrome address bar + task switcher)
    var themeColor = document.createElement('meta');
    themeColor.name = 'theme-color';
    themeColor.content = '#22d3ee';
    head.appendChild(themeColor);

    // iOS / Safari PWA meta tags
    var appleMobileWebAppCapable = document.createElement('meta');
    appleMobileWebAppCapable.name = 'apple-mobile-web-app-capable';
    appleMobileWebAppCapable.content = 'yes';
    head.appendChild(appleMobileWebAppCapable);

    var appleMobileWebAppStatusBar = document.createElement('meta');
    appleMobileWebAppStatusBar.name = 'apple-mobile-web-app-status-bar-style';
    appleMobileWebAppStatusBar.content = 'black-translucent';
    head.appendChild(appleMobileWebAppStatusBar);

    var appleMobileWebAppTitle = document.createElement('meta');
    appleMobileWebAppTitle.name = 'apple-mobile-web-app-title';
    appleMobileWebAppTitle.content = 'Health Coach';
    head.appendChild(appleMobileWebAppTitle);

    // Apple touch icon (uses the existing logo SVG)
    var appleTouchIcon = document.createElement('link');
    appleTouchIcon.rel = 'apple-touch-icon';
    appleTouchIcon.href = '/public/logo.svg';
    head.appendChild(appleTouchIcon);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectPWA);
  } else {
    injectPWA();
  }
})();
