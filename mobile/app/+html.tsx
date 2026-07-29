import { ScrollViewStyleReset } from 'expo-router/html';
import type { ReactNode } from 'react';

// This file is web-only and used to configure the root HTML for every
// web page during static rendering.
// The contents of this function only run in Node.js environments and
// do not have access to the DOM or browser APIs.
export default function Root({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta httpEquiv="X-UA-Compatible" content="IE=edge" />
        <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no" />

        {/*
          Disable body scrolling on web. This makes ScrollView components work closer to how they do on native.
          However, body scrolling is often nice to have for mobile web. If you want to enable it, remove this line.
        */}
        <ScrollViewStyleReset />

        {/* Using raw CSS styles as an escape-hatch to ensure the background color never flickers in dark-mode. */}
        <style dangerouslySetInnerHTML={{ __html: responsiveBackground }} />
        {/* Add any additional <head> elements that you want globally available on web... */}
      </head>
      <body>{children}</body>
    </html>
  );
}

const responsiveBackground = `
body {
  background-color: #fff;
}
@media (prefers-color-scheme: dark) {
  body {
    background-color: #000;
  }
}

/* This app has no desktop breakpoints of its own — it's designed and tested at phone width
   (Android emulator, ~430px web viewport) only. Without this, content and the bottom tab bar
   stretch full-width on a desktop browser, which looks and behaves like nothing was ever
   laid out for that width (because it wasn't). Above 700px, center the app in a fixed
   phone-width frame instead of letting it stretch. */
@media (min-width: 700px) {
  body {
    background-color: #e5e7eb;
    display: flex;
    justify-content: center;
  }
  #root {
    max-width: 430px;
    width: 100%;
    box-shadow: 0 0 40px rgba(0, 0, 0, 0.2);
  }
}
@media (min-width: 700px) and (prefers-color-scheme: dark) {
  body {
    background-color: #111827;
  }
}`;
