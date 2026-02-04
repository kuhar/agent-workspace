import { describe, it, expect } from 'vitest';
import { parseMarksFile, Mark } from './parser';

describe('parseMarksFile', () => {
    const workspaceRoot = '/workspace';

    describe('C++ namespace support', () => {
        it('parses C++ namespaced symbol marks correctly', () => {
            const content = `@mlir::populateVectorToSPIRVPatterns: llvm-project/mlir/lib/Conversion/VectorToSPIRV/VectorToSPIRV.cpp:812`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('@mlir::populateVectorToSPIRVPatterns');
            expect(marks[0].filePath).toBe('/workspace/llvm-project/mlir/lib/Conversion/VectorToSPIRV/VectorToSPIRV.cpp');
            expect(marks[0].line).toBe(812);
        });

        it('parses deeply nested C++ namespaces', () => {
            const content = `@std::chrono::system_clock::now: src/time.cpp:5`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('@std::chrono::system_clock::now');
            expect(marks[0].filePath).toBe('/workspace/src/time.cpp');
            expect(marks[0].line).toBe(5);
        });

        it('handles multiple C++ namespace marks', () => {
            const content = `@mlir::populateVectorToSPIRVPatterns: src/test.cpp:10
@std::chrono::system_clock: src/time.cpp:5
@llvm::StringRef::empty: src/strings.cpp:42`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(3);
            expect(marks[0].name).toBe('@mlir::populateVectorToSPIRVPatterns');
            expect(marks[1].name).toBe('@std::chrono::system_clock');
            expect(marks[2].name).toBe('@llvm::StringRef::empty');
        });
    });

    describe('basic named marks', () => {
        it('parses simple named marks', () => {
            const content = `simple: src/file.ts:20`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('simple');
            expect(marks[0].filePath).toBe('/workspace/src/file.ts');
            expect(marks[0].line).toBe(20);
        });

        it('parses symbol marks with @ prefix', () => {
            const content = `@parseConfig: src/utils.ts:42`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('@parseConfig');
            expect(marks[0].filePath).toBe('/workspace/src/utils.ts');
            expect(marks[0].line).toBe(42);
        });
    });

    describe('anonymous marks', () => {
        it('parses anonymous marks (path:line only)', () => {
            const content = `src/anonymous.ts:30`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBeUndefined();
            expect(marks[0].filePath).toBe('/workspace/src/anonymous.ts');
            expect(marks[0].line).toBe(30);
        });

        it('parses anonymous marks with nested paths', () => {
            const content = `src/components/Button/index.tsx:15`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBeUndefined();
            expect(marks[0].filePath).toBe('/workspace/src/components/Button/index.tsx');
            expect(marks[0].line).toBe(15);
        });
    });

    describe('absolute paths', () => {
        it('preserves absolute paths', () => {
            const content = `mymark: /home/user/project/src/file.ts:100`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('mymark');
            expect(marks[0].filePath).toBe('/home/user/project/src/file.ts');
            expect(marks[0].line).toBe(100);
        });
    });

    describe('comments and empty lines', () => {
        it('skips empty lines', () => {
            const content = `
mark1: src/a.ts:1

mark2: src/b.ts:2
`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(2);
        });

        it('skips hash comments', () => {
            const content = `# This is a comment
mark1: src/a.ts:1
# Another comment
mark2: src/b.ts:2`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(2);
        });

        it('skips HTML comments (single line)', () => {
            const content = `<!-- This is a comment -->
mark1: src/a.ts:1`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
        });

        it('skips HTML comments (multi-line)', () => {
            const content = `<!-- This is a
multi-line
comment -->
mark1: src/a.ts:1`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
        });
    });

    describe('mixed marks', () => {
        it('parses a realistic marks.md file', () => {
            const content = `# Mark and Recall File
# Named marks
mymark: src/utils.ts:10

# Symbol marks
@parseConfig: src/config.ts:42
@mlir::populateVectorToSPIRVPatterns: llvm/lib/file.cpp:812

# Anonymous marks
src/helpers.ts:18
`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(4);
            expect(marks[0]).toEqual({
                name: 'mymark',
                filePath: '/workspace/src/utils.ts',
                line: 10,
            });
            expect(marks[1]).toEqual({
                name: '@parseConfig',
                filePath: '/workspace/src/config.ts',
                line: 42,
            });
            expect(marks[2]).toEqual({
                name: '@mlir::populateVectorToSPIRVPatterns',
                filePath: '/workspace/llvm/lib/file.cpp',
                line: 812,
            });
            expect(marks[3]).toEqual({
                name: undefined,
                filePath: '/workspace/src/helpers.ts',
                line: 18,
            });
        });
    });

    describe('edge cases', () => {
        it('handles whitespace around separators', () => {
            const content = `  mymark:   src/file.ts:10  `;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('mymark');
            expect(marks[0].filePath).toBe('/workspace/src/file.ts');
            expect(marks[0].line).toBe(10);
        });

        it('ignores lines without line numbers', () => {
            const content = `invalid line without colon
mark: src/file.ts:abc
valid: src/file.ts:10`;
            const marks = parseMarksFile(content, workspaceRoot);

            expect(marks).toHaveLength(1);
            expect(marks[0].name).toBe('valid');
        });

        it('handles empty content', () => {
            const marks = parseMarksFile('', workspaceRoot);
            expect(marks).toHaveLength(0);
        });

        it('handles content with only comments', () => {
            const content = `# Just comments
# Nothing else
<!-- HTML comment -->`;
            const marks = parseMarksFile(content, workspaceRoot);
            expect(marks).toHaveLength(0);
        });
    });
});
