// Browser request fixtures based on the bundled Playwright device descriptors.
export const userAgentGroups = [
  {
    label: "Chrome · desktop",
    presets: [
      { label: "Chrome on Windows", value: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
      { label: "Chrome on macOS", value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
      { label: "Chrome on Linux", value: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
    ],
  },
  {
    label: "Edge · desktop",
    presets: [
      { label: "Edge on Windows", value: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36 Edg/158.0.8127.18" },
      { label: "Edge on macOS", value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36 Edg/158.0.8127.18" },
      { label: "Edge on Linux", value: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36 Edg/158.0.8127.18" },
    ],
  },
  {
    label: "Firefox · desktop",
    presets: [
      { label: "Firefox on Windows", value: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) Gecko/20100101 Firefox/159.0" },
      { label: "Firefox on macOS", value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:159.0) Gecko/20100101 Firefox/159.0" },
      { label: "Firefox on Linux", value: "Mozilla/5.0 (X11; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0" },
    ],
  },
  {
    label: "Safari · macOS",
    presets: [
      { label: "Safari on Mac", value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Safari/605.1.15" },
    ],
  },
  {
    label: "Safari · iOS",
    presets: [
      { label: "iPhone 16", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone 15", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone 14", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone 13", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone 12", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 14_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone 11", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone XR", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 12_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
      { label: "iPhone SE (3rd gen)", value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/603.1.30 (KHTML, like Gecko) Version/27.2 Mobile/19E241 Safari/602.1" },
    ],
  },
  {
    label: "Safari · iPadOS",
    presets: [
      { label: "iPad (gen 11)", value: "Mozilla/5.0 (iPad; CPU OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/19E241 Safari/604.1" },
      { label: "iPad (gen 5)", value: "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1" },
    ],
  },
  {
    label: "Chrome · Android phones",
    presets: [
      { label: "Pixel 10", value: "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 10 Pro", value: "Mozilla/5.0 (Linux; Android 16; Pixel 10 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 9", value: "Mozilla/5.0 (Linux; Android 14; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 9 Pro", value: "Mozilla/5.0 (Linux; Android 14; Pixel 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 8", value: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 8 Pro", value: "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 8a", value: "Mozilla/5.0 (Linux; Android 14; Pixel 8a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 7", value: "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 7 Pro", value: "Mozilla/5.0 (Linux; Android 13; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Pixel 6", value: "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Galaxy S24", value: "Mozilla/5.0 (Linux; Android 14; SM-S921U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Galaxy A55", value: "Mozilla/5.0 (Linux; Android 14; SM-A556B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Galaxy Z Fold 7", value: "Mozilla/5.0 (Linux; Android 10; SM-F966U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Galaxy Z Flip 7", value: "Mozilla/5.0 (Linux; Android 10; SM-F761U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Galaxy S9+", value: "Mozilla/5.0 (Linux; Android 8.0.0; SM-G965U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Galaxy S8", value: "Mozilla/5.0 (Linux; Android 7.0; SM-G950U Build/NRD90M) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
      { label: "Moto G4", value: "Mozilla/5.0 (Linux; Android 7.0; Moto G (4)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Mobile Safari/537.36" },
    ],
  },
  {
    label: "Chrome · Android tablets",
    presets: [
      { label: "Galaxy Tab S9", value: "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
      { label: "Galaxy Tab S4", value: "Mozilla/5.0 (Linux; Android 8.1.0; SM-T837A) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
      { label: "Nexus 10", value: "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 10 Build/MOB31T) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
      { label: "Nexus 7", value: "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 7 Build/MOB30X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.8127.18 Safari/537.36" },
    ],
  },
];
