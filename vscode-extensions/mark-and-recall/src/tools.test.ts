import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { detectTools, getTargetDir, AI_TOOLS } from './tools';

describe('detectTools', () => {
    let tmpDir: string;

    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tools-test-'));
    });

    afterEach(() => {
        fs.rmSync(tmpDir, { recursive: true });
    });

    it('returns empty array when no tool dirs exist', () => {
        expect(detectTools(tmpDir)).toEqual([]);
    });

    it('detects Claude Code', () => {
        fs.mkdirSync(path.join(tmpDir, '.claude'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Claude Code');
    });

    it('detects Cursor', () => {
        fs.mkdirSync(path.join(tmpDir, '.cursor'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Cursor');
    });

    it('detects Codex', () => {
        fs.mkdirSync(path.join(tmpDir, '.codex'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Codex');
    });

    it('detects multiple tools', () => {
        fs.mkdirSync(path.join(tmpDir, '.claude'));
        fs.mkdirSync(path.join(tmpDir, '.cursor'));
        fs.mkdirSync(path.join(tmpDir, '.codex'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(3);
        expect(detected.map((t) => t.name)).toEqual(['Claude Code', 'Cursor', 'Codex']);
    });

    it('ignores unrelated directories', () => {
        fs.mkdirSync(path.join(tmpDir, '.vscode'));
        fs.mkdirSync(path.join(tmpDir, '.git'));
        expect(detectTools(tmpDir)).toEqual([]);
    });

    it('accepts custom tools list', () => {
        fs.mkdirSync(path.join(tmpDir, '.custom'));
        const customTools = [
            { name: 'Custom', detection: '.custom', projectDir: '.custom/rules', globalDir: '.custom/rules' },
        ];
        const detected = detectTools(tmpDir, customTools);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Custom');
    });
});

describe('getTargetDir', () => {
    it('returns project dir for project scope', () => {
        const tool = AI_TOOLS[0]; // Claude Code
        expect(getTargetDir(tool, 'project', '/my/project')).toBe('/my/project/.claude/commands');
    });

    it('returns global dir for global scope', () => {
        const tool = AI_TOOLS[0]; // Claude Code
        expect(getTargetDir(tool, 'global', '/home/user')).toBe('/home/user/.claude/commands');
    });

    it('returns correct dirs for Cursor', () => {
        const tool = AI_TOOLS[1]; // Cursor
        expect(getTargetDir(tool, 'project', '/my/project')).toBe('/my/project/.cursor/rules');
        expect(getTargetDir(tool, 'global', '/home/user')).toBe('/home/user/.cursor/rules');
    });

    it('returns correct dirs for Codex', () => {
        const tool = AI_TOOLS[2]; // Codex
        expect(getTargetDir(tool, 'project', '/my/project')).toBe('/my/project/.codex');
        expect(getTargetDir(tool, 'global', '/home/user')).toBe('/home/user/.codex');
    });
});
