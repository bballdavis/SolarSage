const repository = 'SolarSage';
const organization = 'bballdavis';
const github = `https://github.com/${organization}/${repository}`;

/** @type {import('@docusaurus/types').Config} */
module.exports = {
  title: 'SolarSage',
  tagline: 'Make room for the sun.',
  url: `https://${organization}.github.io`,
  baseUrl: `/${repository}/`,
  organizationName: organization,
  projectName: repository,
  trailingSlash: true,
  favicon: 'solarsage-logo.png',
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  staticDirectories: ['../docs/assets'],
  markdown: {format: 'md', mermaid: true, hooks: {onBrokenMarkdownLinks: 'throw'}},
  themes: ['@docusaurus/theme-mermaid'],
  presets: [['classic', {
    docs: {
      path: '../docs',
      routeBasePath: '/',
      sidebarPath: './sidebars.js',
      editUrl: ({docPath}) => `${github}/edit/main/docs/${docPath}`,
      showLastUpdateTime: false,
    },
    blog: false,
    theme: {customCss: './src/css/custom.css'},
  }]],
  themeConfig: {
    image: 'solarsage-wordmark.png',
    colorMode: {defaultMode: 'light', disableSwitch: true, respectPrefersColorScheme: false},
    navbar: {
      logo: {alt: 'SolarSage', src: 'solarsage-wordmark.png', width: 216, height: 72},
      items: [
        {type: 'docSidebar', sidebarId: 'guide', label: 'Documentation', position: 'left'},
        {href: github, label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'light',
      links: [
        {title: 'Start', items: [{label: 'Installation', to: '/installation'}, {label: 'Configuration', to: '/PLANNER'}]},
        {title: 'Understand', items: [{label: 'How it works', to: '/PLAN'}, {label: 'Troubleshooting', to: '/troubleshooting'}]},
        {title: 'Contribute', items: [{label: 'Development', to: '/development'}, {label: 'Report an issue', href: `${github}/issues`}]},
      ],
      copyright: 'SolarSage · A community Home Assistant project. Not affiliated with vehicle or energy equipment manufacturers.',
    },
    prism: {additionalLanguages: ['python', 'bash', 'json', 'yaml']},
  },
};
