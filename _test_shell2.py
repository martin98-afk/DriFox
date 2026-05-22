"""Test shell_compressor more thoroughly"""
import sys
sys.path.insert(0, "D:/work/DriFox")

from app.tools.shell_compressor import compress, _compress_git_status

# More git status variants
print("=== _compress_git_status tests ===")

# Test 1: main branch clean
result = _compress_git_status("On branch main\nnothing to commit, working tree clean")
print(f"clean: {result}")

# Test 2: staged changes
result = _compress_git_status("""On branch feature/auth
Changes to be committed:
  (use "git restore --staged <file>..." to undo)

	new file:   src/auth.ts
	modified:   src/main.rs

Changes not staged for commit:
	modified:   src/config.py
""")
print(f"\nstaged+unstaged:\n{result}")

# Test 3: with ahead
result = _compress_git_status("""On branch develop
Your branch is ahead of 'origin/develop' by 3 commits.
  (use "git push" to push your local commits)

Changes not staged for commit:
	modified:   package.json
	modified:   src/app.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)

  new-feature.js
  temp.txt
""")
print(f"\nahead+untracked:\n{result}")

# Test 4: raw unmodified (should pass through)
result = _compress_git_status("On branch main\nYour branch is up to date with 'origin/main'.\n\nnothing to commit")
print(f"\npass through:\n{result}")

print("\n\n=== compress examples ===")

# git push
print(compress("git push", """Enumerating objects: 5, done.
Counting objects: 100% (5/5), done.
Writing objects: 100% (3/3), 1.2 KiB | 1.2 MiB/s, done.
Total 3 (delta 2), reused 0 (delta 0)
To github.com:user/repo.git
   abc1234..def5678  main -> main"""))

# cargo build
print(compress("cargo build", """   Compiling lean-ctx v2.1.1
    Finished `release` profile [optimized] target(s) in 30.5s"""))

# ruff
print(compress("ruff check .", """src/main.py:10:5 E302 expected 2 blank lines, found 1
src/main.py:15:90 E501 line too long (90 > 79)
src/main.py:20:1 E402 module level import not at top of file
Found 3 errors
Done."""))