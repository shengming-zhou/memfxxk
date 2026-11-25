<script setup lang="ts">
import { useData, withBase } from 'vitepress'
import DefaultTheme from 'vitepress/theme'

const { lang, page } = useData()

const pipeline = {
  en: [
    { title: 'Raw events', detail: 'Conversation and tool output', kind: 'raw' },
    { title: 'Turn records', detail: 'Searchable text with provenance', kind: 'record' },
    { title: 'Vector index', detail: 'Embeddings generated in the background', kind: 'vector' },
  ],
  zh: [
    { title: '原始事件', detail: '保留对话与工具输出', kind: 'raw' },
    { title: 'Turn 记录', detail: '组织为可检索、有来源的文本', kind: 'record' },
    { title: '向量索引', detail: '后台生成嵌入', kind: 'vector' },
  ],
}

function localizedPath(relativePath: string, locale: 'en' | 'zh') {
  const pagePath = relativePath.replace(/^en\//, '')
  const directory = pagePath === 'index.md'
    ? ''
    : pagePath.replace(/\/index\.md$/, '/').replace(/\.md$/, '/')
  return withBase(locale === 'en' ? `/en/${directory}` : `/${directory}`)
}

function preserveSection(event: MouseEvent) {
  const link = event.currentTarget as HTMLAnchorElement
  const legacy = window.location.hash.match(/^#_(\d)$/)
  const section = legacy && document.getElementById(`_${legacy[1]}`)?.nextElementSibling?.id
  link.hash = section || window.location.hash
}
</script>

<template>
  <DefaultTheme.Layout>
    <template #nav-bar-title-before>
      <span class="memory-mark" aria-hidden="true"></span>
    </template>
    <template #nav-bar-content-after>
      <nav class="locale-switch" :aria-label="lang === 'en' ? 'Documentation language' : '文档语言'">
        <a
          lang="zh-CN"
          :aria-current="lang === 'zh-CN' ? 'page' : undefined"
          :href="localizedPath(page.relativePath, 'zh')"
          target="_self"
          @click="preserveSection"
        >中文</a>
        <a
          lang="en"
          aria-label="English"
          :aria-current="lang === 'en' ? 'page' : undefined"
          :href="localizedPath(page.relativePath, 'en')"
          target="_self"
          @click="preserveSection"
        >EN</a>
      </nav>
    </template>
    <template #home-hero-image>
      <div class="pipeline">
        <p class="pipeline-title">{{ lang === 'en' ? 'From event to index' : '从原文到索引' }}</p>
        <ol>
          <li v-for="step in pipeline[lang === 'en' ? 'en' : 'zh']" :key="step.kind" :class="step.kind">
            <span class="pipeline-symbol" aria-hidden="true"></span>
            <div>
              <p class="pipeline-step">{{ step.title }}</p>
              <p class="pipeline-detail">{{ step.detail }}</p>
            </div>
          </li>
        </ol>
      </div>
    </template>
  </DefaultTheme.Layout>
</template>
