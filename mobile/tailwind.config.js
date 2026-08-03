/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{js,jsx,ts,tsx}", "./components/**/*.{js,jsx,ts,tsx}"],
  presets: [require("nativewind/preset")],
  // Required for the in-app Appearance setting (store/themeStore.ts). NativeWind defaults to
  // darkMode: "media", where `dark:` resolves straight from the OS media query and
  // colorScheme.set() throws "Cannot manually set color scheme, as dark mode is type 'media'".
  // With "class" the scheme becomes explicit state that NativeWind still seeds from the OS,
  // so "System" behaves exactly as before while Light/Dark can override it.
  darkMode: "class",
  theme: {
    extend: {},
  },
  plugins: [],
};
