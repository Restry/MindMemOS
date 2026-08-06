import { describe, expect, test } from 'bun:test'
import { eventIdForPair, extractLatestCompletedPair } from './mindmemos-provenance'

describe('Pi/OMP agent_end extraction', () => {
  test('uses only the latest completed user/final-assistant pair from full history', () => {
    const history = [
      { role: 'user', content: 'old question', timestamp: 100 },
      { role: 'assistant', content: [{ type: 'text', text: 'old answer' }], timestamp: 200, stopReason: 'stop' },
      { role: 'user', content: [{ type: 'text', text: 'latest question' }], timestamp: 300 },
      {
        role: 'assistant',
        content: [
          { type: 'thinking', thinking: 'private reasoning' },
          { type: 'toolCall', name: 'read' },
          { type: 'text', text: 'latest final answer' },
        ],
        timestamp: 400,
        responseId: 'response-400',
        stopReason: 'stop',
      },
    ]
    const pair = extractLatestCompletedPair(history)
    expect(pair).not.toBeNull()
    expect(pair?.user).toBe('latest question')
    expect(pair?.assistant).toBe('latest final answer')
    expect(pair?.turnId).toBe('response-400')
  })

  test('deduplicates full-history and latest-pair agent_end payloads', () => {
    const latest = [
      { role: 'user', content: 'same question', timestamp: 500 },
      { role: 'assistant', content: [{ type: 'text', text: 'same answer' }], timestamp: 600, stopReason: 'stop' },
    ]
    const full = [
      { role: 'user', content: 'earlier', timestamp: 100 },
      { role: 'assistant', content: [{ type: 'text', text: 'earlier answer' }], timestamp: 200, stopReason: 'stop' },
      ...latest,
    ]
    const fullPair = extractLatestCompletedPair(full)
    const latestPair = extractLatestCompletedPair(latest)
    expect(fullPair).toEqual(latestPair)
    expect(eventIdForPair('session-1', fullPair!)).toBe(eventIdForPair('session-1', latestPair!))
  })

  test('does not treat tool-use assistant messages as completed turns', () => {
    const pair = extractLatestCompletedPair([
      { role: 'user', content: 'question', timestamp: 100 },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'calling a tool' }],
        timestamp: 200,
        stopReason: 'toolUse',
      },
    ])
    expect(pair).toBeNull()
  })
})
