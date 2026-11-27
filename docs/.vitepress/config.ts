import { defineConfig } from 'vitepress'

export default defineConfig({
  base: '/memfxxk/',
  title: 'memfxxk',
  cleanUrls: true,
  srcExclude: ['architecture.md', 'roadmap.md', 'usage.md', 'codex/**', 'research/**'],
  rewrites: {
    'user-manual.md': 'user-manual/index.md',
    'developer-manual.md': 'developer-manual/index.md',
    'memory-strategy-results.md': 'memory-strategy-results/index.md',
    'en/user-manual.md': 'en/user-manual/index.md',
    'en/developer-manual.md': 'en/developer-manual/index.md',
    'en/memory-strategy-results.md': 'en/memory-strategy-results/index.md',
  },
  locales: {
    root: {
      lang: 'zh-CN',
      description: 'memfxxk 用户手册和开发手册',
      markdown: {
        codeCopyButton: { tooltipText: '复制代码', copiedText: '已复制' },
      },
      themeConfig: {
        nav: [
          { text: '用户手册', link: '/user-manual/', activeMatch: '^/user-manual/' },
          { text: '开发手册', link: '/developer-manual/', activeMatch: '^/developer-manual/' },
        ],
        sidebar: [
          {
            text: '手册',
            items: [
              { text: '用户手册', link: '/user-manual/' },
              { text: '开发手册', link: '/developer-manual/' },
            ],
          },
        ],
        outline: { level: 2, label: '本页目录' },
        docFooter: { prev: '上一篇', next: '下一篇' },
        sidebarMenuLabel: '菜单',
        returnToTopLabel: '返回顶部',
        darkModeSwitchLabel: '外观',
        lightModeSwitchTitle: '切换为浅色模式',
        darkModeSwitchTitle: '切换为深色模式',
        skipToContentLabel: '跳到正文',
      },
    },
    en: {
      lang: 'en',
      description: 'User and developer manuals for memfxxk',
      themeConfig: {
        nav: [
          { text: 'User manual', link: '/en/user-manual/', activeMatch: '^/en/user-manual/' },
          { text: 'Developer manual', link: '/en/developer-manual/', activeMatch: '^/en/developer-manual/' },
        ],
        sidebar: [
          {
            text: 'Manuals',
            items: [
              { text: 'User manual', link: '/en/user-manual/' },
              { text: 'Developer manual', link: '/en/developer-manual/' },
            ],
          },
        ],
        outline: { level: 2, label: 'On this page' },
      },
    },
  },
  themeConfig: {
    socialLinks: [
      { icon: 'github', link: 'https://github.com/shengming-zhou/memfxxk' },
    ],
    search: {
      provider: 'local',
      options: {
        locales: {
          root: {
            translations: {
              button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
              modal: {
                noResultsText: '找不到相关内容',
                resetButtonTitle: '清除搜索',
                backButtonTitle: '关闭搜索',
                displayDetails: '显示详情',
                footer: {
                  selectText: '打开',
                  navigateText: '切换',
                  closeText: '关闭',
                },
              },
            },
          },
        },
      },
    },
  },
})
