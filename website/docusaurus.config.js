// @ts-check
// See: https://docusaurus.io/docs/api/docusaurus-config
import {themes as prismThemes} from 'prism-react-renderer';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'TraceForge',
  tagline: 'Observe. Understand. Control.',
  favicon: 'img/favicon.png',

  future: {
    v4: true,
  },

  // Production URL and base path (GitHub Pages: https://dfinson.github.io/traceforge/).
  url: 'https://dfinson.github.io',
  baseUrl: '/traceforge/',

  organizationName: 'dfinson',
  projectName: 'traceforge',
  trailingSlash: false,

  onBrokenLinks: 'throw',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: './sidebars.js',
          editUrl: 'https://github.com/dfinson/traceforge/tree/main/website/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: 'img/og-card.png',
      colorMode: {
        defaultMode: 'dark',
        respectPrefersColorScheme: false,
      },
      navbar: {
        title: 'TraceForge',
        logo: {
          alt: 'TraceForge',
          src: 'img/logo.png',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'docsSidebar',
            position: 'left',
            label: 'Docs',
          },
          {
            href: 'https://github.com/dfinson/traceforge',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              {label: 'Introduction', to: '/docs/intro'},
              {label: 'Getting Started', to: '/docs/getting-started/installation'},
              {label: 'Governance', to: '/docs/governance/overview'},
              {label: 'Reference', to: '/docs/reference/sources'},
            ],
          },
          {
            title: 'Project',
            items: [
              {label: 'GitHub', href: 'https://github.com/dfinson/traceforge'},
              {label: 'Issues', href: 'https://github.com/dfinson/traceforge/issues'},
              {label: 'SPEC.md', href: 'https://github.com/dfinson/traceforge/blob/main/SPEC.md'},
            ],
          },
          {
            title: 'Ecosystem',
            items: [
              {label: 'CodePlane', href: 'https://github.com/dfinson/codeplane'},
              {label: 'memrelay', href: 'https://github.com/dfinson/memrelay'},
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} TraceForge · MIT License · Built with Docusaurus.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.oneDark,
        additionalLanguages: ['bash', 'powershell', 'yaml', 'python', 'json', 'toml', 'diff'],
      },
    }),
};

export default config;
