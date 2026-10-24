import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PlatformIcon from '@/components/common/PlatformIcon.vue'
import { listPlatformIds } from '@/constants/platformCatalog'
import { getKeyGroupProvider } from '@/utils/keyGroupProviders'
import {
  platformAccentBarClass,
  platformAccentColor,
  platformBadgeClass,
  platformBadgeLightClass,
  platformBorderClass,
  platformBorderStrongClass,
  platformButtonClass,
  platformDiscountClass,
  platformGradientClass,
  platformGradientSubtextClass,
  platformGradientTextClass,
  platformIconClass,
  platformLabel,
  platformTextClass
} from '@/utils/platformColors'

// 自定义多协议平台（custom）的前端登记：配色、图标、分组提供方。
const styleFunctions = {
  platformAccentBarClass,
  platformAccentColor,
  platformBadgeClass,
  platformBadgeLightClass,
  platformBorderClass,
  platformBorderStrongClass,
  platformButtonClass,
  platformDiscountClass,
  platformGradientClass,
  platformGradientSubtextClass,
  platformGradientTextClass,
  platformIconClass,
  platformTextClass
}

describe('custom platform registration', () => {
  it('has its own entry in every platform style table instead of the unknown-platform default', () => {
    for (const [name, fn] of Object.entries(styleFunctions)) {
      expect(fn('custom'), name).not.toBe(fn('not-registered'))
    }
  })

  it('uses a color no other platform occupies', () => {
    const others = [...listPlatformIds().filter(id => id !== 'custom'), 'composite']
    for (const [name, fn] of Object.entries(styleFunctions)) {
      const custom = fn('custom')
      // 十六进制主色与 Tailwind 类名分别比较：custom 的取值不能与任何其它平台相同。
      for (const id of others) {
        expect(fn(id), `${name}/${id}`).not.toBe(custom)
      }
    }
    expect(platformAccentColor('custom')).toBe('#84cc16')
    expect(platformBadgeClass('custom')).toContain('lime')
    for (const id of others) {
      expect(platformBadgeClass(id), id).not.toContain('lime')
    }
  })

  it('is listed in the catalog with its display name', () => {
    expect(listPlatformIds()).toContain('custom')
    expect(platformLabel('custom')).toBe('自定义多协议')
  })

  it('belongs to the generic provider bucket for key groups', () => {
    expect(getKeyGroupProvider('custom')).toBe('other')
  })

  it('renders a dedicated neutral icon rather than the generic fallback', () => {
    const custom = mount(PlatformIcon, { props: { platform: 'custom' } })
    expect(custom.find('path[d="M12 22v-5"]').exists()).toBe(true)
    // 与未登记平台使用的兜底图标不同。
    const fallback = mount(PlatformIcon, { props: { platform: 'not-registered' } })
    expect(fallback.html()).not.toBe(custom.html())
    expect(fallback.find('path[d="M12 22v-5"]').exists()).toBe(false)
  })
})
