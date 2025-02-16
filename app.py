# app.py
from flask import Flask, request, jsonify
from github import Github
import hmac
import hashlib
import os
import json
import base64
from github import GithubIntegration
import requests
from dotenv import load_dotenv
from datetime import datetime
import traceback

# Load environment variables
load_dotenv()

# comment to trigger PR review 2

app = Flask(__name__)

# GitHub App credentials
GITHUB_APP_ID = os.getenv('GITHUB_APP_ID')
GITHUB_PRIVATE_KEY = os.getenv('GITHUB_PRIVATE_KEY')
GITHUB_WEBHOOK_SECRET = os.getenv('GITHUB_WEBHOOK_SECRET')
MISTRAL_API_KEY = os.getenv('MISTRAL_API_KEY')

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# comment to trigger PR review

def verify_webhook(request):
    signature = request.headers.get('X-Hub-Signature-256')
    if not signature:
        print("No signature found in request")
        return False
    
    if not GITHUB_WEBHOOK_SECRET:
        print("No webhook secret configured")
        return False
    
    expected_signature = 'sha256=' + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode('utf-8'),
        request.data,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected_signature)

def get_github_client(installation_id):
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        raise ValueError("GitHub App credentials not configured")
        
    try:
        print(f"Creating GitHub integration with App ID: {GITHUB_APP_ID}")
        integration = GithubIntegration(
            GITHUB_APP_ID,
            GITHUB_PRIVATE_KEY.replace('\\n', '\n')  # Fix newline encoding
        )
        
        print(f"Getting access token for installation ID: {installation_id}")
        access_token = integration.get_access_token(installation_id).token
        print("Successfully got access token")
        
        return Github(access_token)
    except Exception as e:
        print(f"Error in get_github_client: {str(e)}")
        print(f"Error type: {type(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        raise

def analyze_code(file_changes, file_name):
    if not MISTRAL_API_KEY:
        return "Error: Mistral API key not configured"
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MISTRAL_API_KEY}"
    }
    
    # Truncate file changes if too long
    max_changes_length = 4000
    if len(file_changes) > max_changes_length:
        file_changes = file_changes[:max_changes_length] + "\n... (truncated for length)"
    
    system_prompt = """You are an expert software developer conducting code reviews.
    You will be shown a git patch/diff of code changes.
    Lines starting with '+' are additions and lines starting with '-' are deletions.
    ONLY review the specific changes shown in the diff - do not make assumptions about other parts of the code.
    Provide concise, actionable feedback focusing on code quality, best practices, and potential improvements.
    Format your review in clear sections for positive aspects and suggestions.
    Keep your response brief and focused."""
    
    user_prompt = f"""Review these specific changes in {file_name}:

The following shows the git diff of changes made:
{file_changes}

Focus ONLY on analyzing the changed lines (marked with + or -) for:
1. Good practices and improvements implemented
2. Potential issues or areas for improvement
3. Security concerns if any
4. Performance considerations

Provide your review in this format:
1. Positive points: [Brief list of good implementations in the changes]
2. Key suggestions: [Prioritized list of improvements for the changes]
3. Code example: [If applicable, show a brief example of suggested improvement]
4. Summary: [One-line overview of the specific changes made]"""

    data = {
        "model": "mistral-large-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 800,
        "temperature": 0.7
    }
    
    try:
        print(f"Sending request to Mistral API for {file_name}")
        
        response = requests.post(
            MISTRAL_API_URL, 
            headers=headers, 
            json=data,
            timeout=30  # 30 seconds timeout
        )
        
        print(f"Got response from Mistral API for {file_name}")
        print(f"Response status code: {response.status_code}")
        
        try:
            response.raise_for_status()
            response_json = response.json()
            
            if not response_json.get("choices"):
                print("No choices in response")
                return "Error: Invalid response from code analysis service"
                
            review = response_json["choices"][0]["message"]["content"]
            
            # Truncate review if too long
            max_review_length = 3000
            if len(review) > max_review_length:
                review = review[:max_review_length] + "\n... (truncated for length)"
                
            return review
            
        except ValueError as json_err:
            print(f"JSON parsing error: {str(json_err)}")
            return "Error: Invalid response format from code analysis service"
            
    except requests.exceptions.Timeout:
        print(f"Timeout while analyzing {file_name}")
        return "Error: Code analysis service timeout"
        
    except requests.exceptions.RequestException as e:
        print(f"Request error in analyze_code: {str(e)}")
        return f"Error analyzing code: Request failed"
        
    except Exception as e:
        print(f"Unexpected error in analyze_code: {str(e)}")
        print(f"Error type: {type(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return "Error: Unexpected error during code analysis"

def get_last_review_timestamp(pull):
    """Get timestamp of the last review comment by the bot"""
    try:
        print("Getting previous review comments")
        comments = pull.get_issue_comments()
        for comment in reversed(list(comments)):
            if comment.user.type == 'Bot' and ('Code Review for Latest Changes' in comment.body or 'Initial Code Review for PR' in comment.body):
                print(f"Found last review timestamp: {comment.created_at}")
                return comment.created_at
        print("No previous review found")
        return None
    except Exception as e:
        print(f"Error getting last review timestamp: {str(e)}")
        print(f"Error type: {type(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return None

def get_files_from_commits(commits):
    """Get all unique files changed in the given commits"""
    try:
        files_changed = set()
        for commit in commits:
            print(f"Getting files from commit: {commit.sha[:7]}")
            for file in commit.files:
                files_changed.add(file.filename)
        print(f"Total unique files changed: {len(files_changed)}")
        return files_changed
    except Exception as e:
        print(f"Error in get_files_from_commits: {str(e)}")
        print(f"Error type: {type(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        raise

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        print("\n=== Webhook Request Data ===")
        print("Headers:", dict(request.headers))
        
        if not verify_webhook(request):
            print("Webhook verification failed")
            return jsonify({'error': 'Invalid signature'}), 403

        event = request.headers.get('X-GitHub-Event')
        print(f"Event type: {event}")
        
        if event != 'pull_request':
            return jsonify({'status': 'skipped', 'reason': f'Event {event} not handled'}), 200

        payload = request.json
        action = payload['action']
        print(f"Action: {action}")
        
        if action not in ['opened', 'synchronize']:
            return jsonify({'status': 'skipped', 'reason': f'Action {action} not handled'}), 200

        try:
            installation_id = payload['installation']['id']
            repo_name = payload['repository']['full_name']
            pr_number = payload['pull_request']['number']
            
            print(f"\n=== Processing PR ===")
            print(f"Repository: {repo_name}")
            print(f"PR Number: {pr_number}")
            
            gh = get_github_client(installation_id)
            repo = gh.get_repo(repo_name)
            pull = repo.get_pull(pr_number)

            reviews = []
            review_errors = []
            
            if action == 'opened':
                files_to_review = list(pull.get_files())
                commits = list(pull.get_commits())
                commit_shas = [c.sha[:7] for c in commits]
                review_header = f"# Initial Code Review for PR\nReviewing all commits: {', '.join(commit_shas)}\n\n"
            else:
                last_review_time = get_last_review_timestamp(pull)
                all_commits = list(pull.get_commits())
                
                if last_review_time:
                    new_commits = [
                        commit for commit in all_commits
                        if commit.commit.author.date > last_review_time
                    ]
                else:
                    new_commits = [all_commits[-1]]
                
                latest_files = get_files_from_commits(new_commits)
                files_to_review = [
                    file for file in pull.get_files()
                    if file.filename in latest_files
                ]
                
                commit_shas = [commit.sha[:7] for commit in new_commits]
                review_header = f"# Code Review for Latest Changes\nReviewing commits: {', '.join(commit_shas)}\n\n"
            
            # Process files in smaller batches
            batch_size = 3
            for i in range(0, len(files_to_review), batch_size):
                batch = files_to_review[i:i + batch_size]
                
                for file in batch:
                    try:
                        if not file.patch:
                            continue
                            
                        changes = "```diff\n" + file.patch + "\n```"
                        review_comment = analyze_code(changes, file.filename)
                        
                        if review_comment.startswith("Error:"):
                            review_errors.append(f"Error reviewing `{file.filename}`: {review_comment}")
                        else:
                            reviews.append(f"### Review for `{file.filename}`:\n\n{review_comment}\n\n---\n\n")
                            
                    except Exception as e:
                        print(f"Error processing {file.filename}: {str(e)}")
                        review_errors.append(f"Error reviewing `{file.filename}`: {str(e)}")

            if reviews or review_errors:
                combined_review = review_header
                
                if reviews:
                    combined_review += "".join(reviews)
                    
                if review_errors:
                    combined_review += "\n### Review Errors:\n" + "\n".join(review_errors)
                
                try:
                    print("Posting review comment...")
                    pull.create_issue_comment(combined_review)
                    print("Successfully posted review")
                except Exception as e:
                    print(f"Error posting comment: {str(e)}")
                    # Try posting a shorter version if the comment is too long
                    if len(combined_review) > 65536:  # GitHub's comment length limit
                        truncated_review = combined_review[:65000] + "\n\n... (Review truncated due to length)"
                        pull.create_issue_comment(truncated_review)
                        print("Posted truncated review")

            return jsonify({'status': 'success'}), 200
            
        except Exception as e:
            print(f"Error in GitHub operations: {str(e)}")
            raise
            
    except Exception as e:
        print(f"Fatal Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return "PR Review Bot is running!"

if __name__ == '__main__':
    app.run(port=3000, debug=True)