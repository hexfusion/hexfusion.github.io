.PHONY: help serve preview build clean spellcheck wordcount new

CONTENT_DIR := content/posts
DRAFT_FLAG  := --buildDrafts

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

serve: ## Preview site locally with drafts (http://localhost:1313)
	hugo server $(DRAFT_FLAG) --navigateToChanged

preview: ## Preview site locally without drafts (production view)
	hugo server --navigateToChanged

build: ## Build site to ./public
	hugo --minify

build-drafts: ## Build site including drafts
	hugo --minify $(DRAFT_FLAG)

clean: ## Remove generated site
	rm -rf public/ resources/_gen/

spellcheck: ## Run spellcheck on all posts
	@echo "Spellchecking posts..."
	@for f in $(CONTENT_DIR)/*.md; do \
		echo "\n--- $$f ---"; \
		sed -e '/^---$$/,/^---$$/d' \
		    -e 's/```[^`]*```//g' \
		    -e 's/`[^`]*`//g' \
		    -e 's/\[.*\](.*)//' \
		    -e 's/^#.*$$//' \
		    "$$f" | \
		hunspell -l -d en_US | sort -u; \
	done
	@echo "\n(Words listed above are not in dictionary. Add to .wordlist to ignore.)"

spellcheck-file: ## Spellcheck a single file: make spellcheck-file FILE=content/posts/foo.md
	@test -n "$(FILE)" || (echo "Usage: make spellcheck-file FILE=path/to/post.md" && exit 1)
	@echo "--- $(FILE) ---"
	@sed -e '/^---$$/,/^---$$/d' \
	     -e 's/```[^`]*```//g' \
	     -e 's/`[^`]*`//g' \
	     -e 's/\[.*\](.*)//' \
	     -e 's/^#.*$$//' \
	     "$(FILE)" | \
	hunspell -l -d en_US | sort -u
	@echo "\n(Add false positives to .wordlist)"

wordcount: ## Show word count for all posts
	@echo "Word counts:"
	@for f in $(CONTENT_DIR)/*.md; do \
		words=$$(sed '/^---$$/,/^---$$/d' "$$f" | wc -w); \
		printf "  %6s  %s\n" "$$words" "$$f"; \
	done

drafts: ## List all draft posts
	@echo "Draft posts:"
	@grep -rl 'draft: true' $(CONTENT_DIR)/ 2>/dev/null | while read f; do \
		title=$$(grep '^title:' "$$f" | head -1 | sed 's/title: *"*//;s/"*$$//'); \
		printf "  %-60s %s\n" "$$f" "$$title"; \
	done

published: ## List all published posts
	@echo "Published posts:"
	@grep -rL 'draft: true' $(CONTENT_DIR)/ 2>/dev/null | while read f; do \
		title=$$(grep '^title:' "$$f" | head -1 | sed 's/title: *"*//;s/"*$$//'); \
		printf "  %-60s %s\n" "$$f" "$$title"; \
	done

new: ## Create a new post: make new SLUG=my-post-title
	@test -n "$(SLUG)" || (echo "Usage: make new SLUG=my-post-title" && exit 1)
	hugo new posts/$(SLUG).md
	@echo "Created: $(CONTENT_DIR)/$(SLUG).md"
