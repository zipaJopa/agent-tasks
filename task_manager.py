#!/usr/bin/env python3
"""
Agent Task Manager - GitHub Native Execution Engine
---------------------------------------------------
This script is designed to be run by a GitHub Actions workflow.
It expects GITHUB_TOKEN and ISSUE_NUMBER as environment variables.

Responsibilities:
1. Fetches task details from the assigned GitHub Issue.
2. Parses task JSON from the issue body.
3. Executes the specified task type by dispatching to a handler.
4. Commits task results to the 'agent-results' repository.
5. Updates task status (comments, labels, close issue) in 'agent-tasks'.
6. Queues relevant information for embedding in the 'agent-memory' system.
7. Handles errors and reports them.
"""

import os
import json
import time
import requests
import base64
from datetime import datetime, timezone

# --- Configuration Constants ---
GITHUB_API_URL = "https://api.github.com"
OWNER = "zipaJopa"  # Your GitHub username or organization
AGENT_TASKS_REPO_NAME = "agent-tasks"
AGENT_RESULTS_REPO_NAME = "agent-results"
AGENT_MEMORY_REPO_NAME = "agent-memory"

AGENT_TASKS_REPO_FULL = f"{OWNER}/{AGENT_TASKS_REPO_NAME}"
AGENT_RESULTS_REPO_FULL = f"{OWNER}/{AGENT_RESULTS_REPO_NAME}"
AGENT_MEMORY_REPO_FULL = f"{OWNER}/{AGENT_MEMORY_REPO_NAME}"

# --- GitHub Interaction Helper Class ---
class GitHubInteraction:
    def __init__(self, token):
        self.token = token
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def _request(self, method, endpoint, data=None, params=None, max_retries=3, base_url=GITHUB_API_URL):
        url = f"{base_url}{endpoint}"
        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, params=params, json=data)
                # print(f"DEBUG: Request to {url} with status {response.status_code}")
                # if data: print(f"DEBUG: Request data: {json.dumps(data)[:200]}")
                # if response.content: print(f"DEBUG: Response content: {response.content[:200]}")
                
                if 'X-RateLimit-Remaining' in response.headers and int(response.headers['X-RateLimit-Remaining']) < 10:
                    reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
                    sleep_duration = max(0, reset_time - time.time()) + 5
                    print(f"Rate limit low. Sleeping for {sleep_duration:.2f} seconds.")
                    time.sleep(sleep_duration)

                response.raise_for_status()
                return response.json() if response.content else {}
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 403 and "rate limit exceeded" in e.response.text.lower():
                    reset_time = int(e.response.headers.get('X-RateLimit-Reset', time.time() + 60 * (attempt + 1)))
                    sleep_duration = max(0, reset_time - time.time()) + 5
                    print(f"Rate limit exceeded. Retrying in {sleep_duration:.2f}s (attempt {attempt+1}/{max_retries}).")
                    time.sleep(sleep_duration)
                    continue
                elif e.response.status_code == 404 and method == "GET": # For GETs, 404 might be a valid "not found"
                    print(f"Resource not found (404) at {url}")
                    return None
                elif e.response.status_code == 422 and "No commit found for SHA" in e.response.text: # Trying to update non-existent file
                     print(f"File not found for update (422) at {url}. Will attempt to create.")
                     return {"error": "file_not_found_for_update", "sha": None} # Special case for commit_file
                print(f"GitHub API request failed ({method} {url}): {e.response.status_code} - {e.response.text}")
                if attempt == max_retries - 1:
                    raise
            except requests.exceptions.RequestException as e:
                print(f"GitHub API request failed ({method} {url}): {e}")
                if attempt == max_retries - 1:
                    raise
            time.sleep(2 ** attempt)
        return {}

    def get_issue(self, repo_full_name, issue_number):
        print(f"Fetching issue #{issue_number} from {repo_full_name}...")
        return self._request("GET", f"/repos/{repo_full_name}/issues/{issue_number}")

    def post_comment(self, repo_full_name, issue_number, body):
        print(f"Posting comment to issue #{issue_number} in {repo_full_name}...")
        return self._request("POST", f"/repos/{repo_full_name}/issues/{issue_number}/comments", data={"body": body})

    def add_labels(self, repo_full_name, issue_number, labels):
        print(f"Adding labels {labels} to issue #{issue_number} in {repo_full_name}...")
        return self._request("POST", f"/repos/{repo_full_name}/issues/{issue_number}/labels", data={"labels": labels})

    def remove_label(self, repo_full_name, issue_number, label_name):
        print(f"Removing label '{label_name}' from issue #{issue_number} in {repo_full_name}...")
        # The API returns 200 OK even if the label doesn't exist, or 404 if no labels on issue.
        # We'll suppress 404 for this specific operation if it means "label not found".
        try:
            return self._request("DELETE", f"/repos/{repo_full_name}/issues/{issue_number}/labels/{label_name}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                print(f"Label '{label_name}' not found or no labels on issue #{issue_number}. Continuing.")
                return {} # Treat as success
            raise


    def close_issue(self, repo_full_name, issue_number):
        print(f"Closing issue #{issue_number} in {repo_full_name}...")
        return self._request("PATCH", f"/repos/{repo_full_name}/issues/{issue_number}", data={"state": "closed"})

    def commit_file(self, repo_full_name, file_path, content_str, commit_message, branch="main"):
        print(f"Committing file '{file_path}' to {repo_full_name} on branch '{branch}'...")
        encoded_content = base64.b64encode(content_str.encode('utf-8')).decode('utf-8')
        
        # Check if file exists to get its SHA for update
        get_file_endpoint = f"/repos/{repo_full_name}/contents/{file_path}?ref={branch}"
        existing_file_data = self._request("GET", get_file_endpoint)
        
        payload = {
            "message": commit_message,
            "content": encoded_content,
            "branch": branch
        }
        
        if existing_file_data and "sha" in existing_file_data :
            payload["sha"] = existing_file_data["sha"]
        elif existing_file_data and existing_file_data.get("error") == "file_not_found_for_update":
             pass # Will create new file
        elif existing_file_data is None: # GET returned 404, means file doesn't exist
            pass # Will create new file
        
        put_endpoint = f"/repos/{repo_full_name}/contents/{file_path}"
        response = self._request("PUT", put_endpoint, data=payload)
        if response and "content" in response and "html_url" in response["content"]:
            return response["content"]["html_url"]
        return None

# --- GitHub Harvester (Integrated) ---
class GitHubHarvester:
    def __init__(self, github_interaction_instance):
        self.gh = github_interaction_instance # Use the shared GitHubInteraction instance
        
    def harvest_trending_projects(self, topics=None, min_stars=10, created_after="2024-01-01", count_per_topic=3):
        print("⚡ Harvesting trending GitHub projects...")
        if topics is None:
            topics = ['ai-agent', 'automation', 'saas-template', 'trading-bot', 'data-engineering', 'devops-tools']
        
        all_harvested_projects = []
        for topic in topics:
            print(f"Searching for topic: {topic}")
            repos = self.search_repos_by_topic(topic, min_stars, created_after, count_per_topic)
            for repo_data in repos:
                analyzed_project = self.analyze_project(repo_data)
                all_harvested_projects.append(analyzed_project)
                print(f"📦 Harvested: {analyzed_project['name']} (Stars: {analyzed_project['stars']}, Lang: {analyzed_project['language']})")
            time.sleep(1) # Be respectful to API limits between topics
        return all_harvested_projects
    
    def search_repos_by_topic(self, topic, min_stars, created_after, count_per_topic):
        query = f'topic:{topic} stars:>{min_stars} created:>{created_after}'
        params = {
            'q': query,
            'sort': 'stars',
            'order': 'desc',
            'per_page': count_per_topic
        }
        response_data = self.gh._request("GET", "/search/repositories", params=params)
        return response_data.get('items', []) if response_data else []
    
    def analyze_project(self, repo_data):
        return {
            'id': repo_data['id'],
            'name': repo_data['name'],
            'full_name': repo_data['full_name'],
            'url': repo_data['html_url'],
            'description': repo_data.get('description'),
            'stars': repo_data['stargazers_count'],
            'forks': repo_data['forks_count'],
            'language': repo_data.get('language'),
            'topics': repo_data.get('topics', []),
            'created_at': repo_data['created_at'],
            'updated_at': repo_data['updated_at'],
            'pushed_at': repo_data['pushed_at'],
            'license': repo_data.get('license', {}).get('name') if repo_data.get('license') else None,
            'harvested_at': datetime.now(timezone.utc).isoformat(),
        }

# --- Task Handler Functions ---
def handle_harvest_task(payload, task_id, gh_interaction):
    print(f"Executing 'harvest' task (ID: {task_id}). Payload: {payload}")
    harvester = GitHubHarvester(gh_interaction)
    
    # Extract parameters from payload or use defaults
    topics = payload.get('topics', ['ai', 'agent', 'automation', 'llm'])
    min_stars = payload.get('min_stars', 50)
    created_after = payload.get('created_after', (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d'))
    count_per_topic = payload.get('count_per_topic', 5)

    harvested_data = harvester.harvest_trending_projects(
        topics=topics, 
        min_stars=min_stars, 
        created_after=created_after, 
        count_per_topic=count_per_topic
    )
    return {"status": "success", "count": len(harvested_data), "harvested_projects": harvested_data}

# Placeholder handlers for other task types
def generic_task_handler(task_type, payload, task_id, gh_interaction):
    print(f"Executing '{task_type}' task (ID: {task_id}). Payload: {payload}")
    time.sleep(2) # Simulate work
    # In a real scenario, this would call specific logic for the task type
    # For example, for 'arbitrage', it might clone a repo, modify it, and create a PR.
    # For 'wrapper', it might generate an API wrapper.
    return {"status": "success", "message": f"Task '{task_type}' (ID: {task_id}) processed.", "data_summary": f"Input payload keys: {list(payload.keys())}"}

# --- Result and Memory Management ---
def store_task_result(task_id, task_type, result_data, gh_interaction):
    print(f"Storing result for task {task_id} (Type: {task_type})...")
    result_json_str = json.dumps(result_data, indent=2)
    
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    file_path = f"outputs/{date_str}/{task_type}_{task_id.replace(':', '_')}.json" # Sanitize task_id if needed
    commit_message = f"feat: Store results for {task_type} task {task_id}"
    
    result_url = gh_interaction.commit_file(AGENT_RESULTS_REPO_FULL, file_path, result_json_str, commit_message)
    if result_url:
        print(f"Result stored at: {result_url}")
        return result_url
    else:
        print(f"Failed to store result for task {task_id}.")
        return None

def queue_for_memory_embedding(task_id, task_type, content_items, source_ref, gh_interaction):
    """
    Queues content for embedding by committing it to the agent-memory repository.
    content_items: A list of dictionaries, e.g., [{"text": "...", "id": "item1"}, ...]
                   or a single string for one item.
    source_ref: URL or identifier for the source of the content.
    """
    print(f"Queueing content from task {task_id} (Type: {task_type}) for memory embedding...")
    
    if isinstance(content_items, str): # Single text item
        items_to_embed = [{"id": f"{task_id}_item_0", "text": content_items}]
    elif isinstance(content_items, list):
        items_to_embed = []
        for i, item in enumerate(content_items):
            if isinstance(item, str):
                items_to_embed.append({"id": f"{task_id}_item_{i}", "text": item})
            elif isinstance(item, dict) and "text" in item:
                 # Ensure item has a unique ID or generate one
                item_id = item.get("id", f"{task_id}_item_{i}")
                items_to_embed.append({"id": item_id, "text": item["text"], "metadata": item.get("metadata", {})})
            else:
                print(f"Warning: Skipping invalid content item for embedding: {item}")
                continue
    else:
        print(f"Warning: Invalid content_items format for embedding: {type(content_items)}. Must be str or list.")
        return None

    if not items_to_embed:
        print("No valid items to queue for embedding.")
        return None

    payload = {
        "task_id": task_id,
        "task_type": task_type,
        "source_ref": source_ref,
        "items": items_to_embed,
        "queued_at": datetime.now(timezone.utc).isoformat()
    }
    payload_json_str = json.dumps(payload, indent=2)
    
    # Create a unique filename, e.g., using timestamp or a hash of task_id
    timestamp_slug = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    file_path = f"pending_embeddings/{task_type}_{task_id.replace(':', '_')}_{timestamp_slug}.json"
    commit_message = f"feat: Queue content for embedding from {task_type} task {task_id}"
    
    queue_file_url = gh_interaction.commit_file(AGENT_MEMORY_REPO_FULL, file_path, payload_json_str, commit_message)
    if queue_file_url:
        print(f"Content queued for embedding at: {queue_file_url}")
        return queue_file_url
    else:
        print(f"Failed to queue content for embedding for task {task_id}.")
        return None

# --- Main Execution Logic ---
def main():
    github_token = os.getenv("GITHUB_TOKEN")
    issue_number_str = os.getenv("ISSUE_NUMBER")

    if not github_token:
        print("Error: GITHUB_TOKEN environment variable not set.")
        return
    if not issue_number_str:
        print("Error: ISSUE_NUMBER environment variable not set.")
        return
    
    try:
        issue_number = int(issue_number_str)
    except ValueError:
        print(f"Error: ISSUE_NUMBER '{issue_number_str}' is not a valid integer.")
        return

    gh_interaction = GitHubInteraction(github_token)

    # 1. Fetch task issue
    issue_data = gh_interaction.get_issue(AGENT_TASKS_REPO_FULL, issue_number)
    if not issue_data:
        print(f"Error: Could not fetch issue #{issue_number}.")
        # Potentially create an alert or a new issue in agent-controller repo
        return

    # 2. Parse task JSON from issue body
    task_id = None
    task_type = None
    try:
        task_details_json = issue_data.get("body", "{}")
        if not task_details_json.strip(): # Handle empty body
             task_details_json = "{}"
        task_details = json.loads(task_details_json)
        task_id = task_details.get("id", f"unknown_id_{issue_number}")
        task_type = task_details.get("type", "unknown_type")
        task_payload = task_details.get("payload", {})
        print(f"Successfully parsed task: ID={task_id}, Type={task_type}")
    except json.JSONDecodeError as e:
        err_msg = f"Error parsing task JSON from issue #{issue_number} body: {e}. Body: '{issue_data.get('body', '')[:200]}...'"
        print(err_msg)
        gh_interaction.post_comment(AGENT_TASKS_REPO_FULL, issue_number, f"❌ Task failed: Could not parse task JSON.\nError: {e}")
        gh_interaction.add_labels(AGENT_TASKS_REPO_FULL, issue_number, ["failed", "invalid-json"])
        return

    # 3. Post "Task started" comment
    start_comment = f"🚀 Task `{task_id}` (Type: `{task_type}`) started execution by `{os.getenv('GITHUB_ACTOR', 'task-manager-bot')}`."
    gh_interaction.post_comment(AGENT_TASKS_REPO_FULL, issue_number, start_comment)

    task_result_data = None
    error_occurred = False
    error_message = ""

    # 4. Execute task
    try:
        if task_type == "harvest":
            task_result_data = handle_harvest_task(task_payload, task_id, gh_interaction)
        # Add elif blocks for all other specific task types from AGENT_MAPPING in agent_controller.py
        # Example:
        # elif task_type == "arbitrage":
        #     task_result_data = generic_task_handler(task_type, task_payload, task_id, gh_interaction)
        # ... and so on for all ~20 agent types
        elif task_type in ["arbitrage", "wrapper", "saas_template", "automation_broker", 
                           "trade_signal", "self_healing", "performance_optimization", 
                           "financial_management", "crypto_degen", "influencer_farm", 
                           "course_generator", "patent_scraper", "domain_flipper", 
                           "affiliate_army", "lead_magnet", "copywriter_swarm", 
                           "price_scraper", "startup_idea"]:
            task_result_data = generic_task_handler(task_type, task_payload, task_id, gh_interaction)
        else:
            error_message = f"Unknown task type: '{task_type}'"
            print(f"Error: {error_message}")
            error_occurred = True
            task_result_data = {"status": "error", "message": error_message}

        if task_result_data.get("status") == "error" and not error_occurred: # Error reported by handler
            error_message = task_result_data.get("message", "Task handler reported an error.")
            error_occurred = True

    except Exception as e:
        error_message = f"An unexpected error occurred during task execution: {str(e)}"
        print(f"Error: {error_message}")
        import traceback
        traceback.print_exc()
        error_occurred = True
        task_result_data = {"status": "error", "message": error_message, "traceback": traceback.format_exc()}
    
    # 5. Handle result/error
    if not error_occurred:
        print(f"Task {task_id} completed successfully.")
        # Store result
        result_file_url = store_task_result(task_id, task_type, task_result_data, gh_interaction)
        
        # Queue for memory (example for harvest task)
        if task_type == "harvest" and task_result_data.get("harvested_projects"):
            content_for_memory = []
            for proj in task_result_data["harvested_projects"][:5]: # Embed first 5 for demo
                text = f"Project: {proj['full_name']}\nDescription: {proj['description']}\nLanguage: {proj['language']}\nTopics: {', '.join(proj['topics'])}"
                content_for_memory.append({"text": text, "id": proj['url'], "metadata": {"source": "github_harvest", "stars": proj['stars']}})
            if content_for_memory:
                 queue_for_memory_embedding(task_id, task_type, content_for_memory, AGENT_TASKS_REPO_FULL + f"/issues/{issue_number}", gh_interaction)

        # Post "DONE" comment
        done_comment = f"DONE ✅ Task `{task_id}` (Type: `{task_type}`) completed successfully."
        if result_file_url:
            done_comment += f"\nResults stored at: {result_file_url}"
        gh_interaction.post_comment(AGENT_TASKS_REPO_FULL, issue_number, done_comment)
        
        # Update labels and close issue
        gh_interaction.remove_label(AGENT_TASKS_REPO_FULL, issue_number, "in-progress")
        gh_interaction.add_labels(AGENT_TASKS_REPO_FULL, issue_number, ["completed"])
        gh_interaction.close_issue(AGENT_TASKS_REPO_FULL, issue_number)
    else:
        print(f"Task {task_id} failed.")
        # Store error details if needed (optional, could just be in comment)
        # store_task_result(task_id, task_type, task_result_data, gh_interaction) # If you want to store error details too

        fail_comment = f"❌ Task `{task_id}` (Type: `{task_type}`) failed.\nError: {error_message[:1000]}" # Limit error message length for comment
        gh_interaction.post_comment(AGENT_TASKS_REPO_FULL, issue_number, fail_comment)
        gh_interaction.add_labels(AGENT_TASKS_REPO_FULL, issue_number, ["failed", "needs-review"])
        # Do not close failed issues automatically, let controller or human review.

    print(f"Task manager finished for issue #{issue_number}.")

if __name__ == "__main__":
    main()
