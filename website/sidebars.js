// @ts-check
// Manual sidebar for the TraceForge docs. The navbar references `docsSidebar`.

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  docsSidebar: [
    'intro',
    {
      type: 'category',
      label: 'Architecture',
      collapsed: false,
      items: ['architecture/overview', 'architecture/event-model'],
    },
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: [
        'getting-started/installation',
        'getting-started/first-run',
        'getting-started/cli',
      ],
    },
    'configuration',
    {
      type: 'category',
      label: 'Governance',
      items: [
        'governance/overview',
        'governance/extensions',
        'governance/gate',
      ],
    },
    {
      type: 'category',
      label: 'Reference',
      items: [
        'reference/sources',
        'reference/adapters',
        'reference/enrichment',
        'reference/classification',
        'reference/pipeline',
        'reference/sinks',
        'reference/live-structuring',
        'reference/sdk',
      ],
    },
  ],
};

export default sidebars;
