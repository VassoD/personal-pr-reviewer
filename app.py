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

app = Flask(__name__)

# GitHub App credentials
GITHUB_APP_ID = os.getenv('GITHUB_APP_ID')
GITHUB_PRIVATE_KEY = os.getenv('GITHUB_PRIVATE_KEY')
GITHUB_WEBHOOK_SECRET = os.getenv('GITHUB_WEBHOOK_SECRET')
MISTRAL_API_KEY = os.getenv('MISTRAL_API_KEY')

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

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

    try:
        # If file is too large, return early with a message
        if len(file_changes) > 10000:  # Conservative limit
            return "File changes too large for detailed review. Key changes include modifications to code structure and functionality."

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MISTRAL_API_KEY}"
        }
        
        # Aggressively truncate file changes
        max_changes_length = 3000  # More conservative limit
        if len(file_changes) > max_changes_length:
            # Count the number of changes
            change_lines = [line for line in file_changes.split('\n') if line.startswith('+') or line.startswith('-')]
            total_changes = len(change_lines)
            
            truncated_changes = file_changes[:max_changes_length]
            truncated_changes = truncated_changes[:truncated_changes.rindex('\n')]  # Cut at last complete line
            truncated_changes += f"\n... (truncated, {total_changes} total changes)"
            file_changes = truncated_changes

        system_prompt = """You are an expert software developer conducting code reviews.
        Provide a VERY BRIEF review focusing only on the most important aspects.
        Format your review in clear sections for positive aspects and suggestions."""

        user_prompt = f"""Review these changes in {file_name}:
{file_changes}

Provide a BRIEF review with:
1. Positive points (2-3 only)
2. Key suggestions (2-3 only)
3. One-line summary"""

        data = {
            "model": "mistral-large-latest",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 500,  # Reduced token limit
            "temperature": 0.7
        }
        
        try:
            print(f"Sending request to Mistral API for {file_name}")
            
            response = requests.post(
                MISTRAL_API_URL, 
                headers=headers, 
                json=data,
                timeout=20  # Reduced timeout
            )
            
            print(f"Got response from Mistral API for {file_name}")
            print(f"Response status code: {response.status_code}")
            
            try:
                response.raise_for_status()
                response_json = response.json()
                
                if not response_json.get("choices"):
                    return "Error: Invalid response from code analysis service"
                    
                review = response_json["choices"][0]["message"]["content"]
                
                # Strictly limit review length
                max_review_length = 2000
                if len(review) > max_review_length:
                    review = review[:max_review_length] + "\n... (truncated)"
                    
                return review
                
            except ValueError as json_err:
                print(f"JSON parsing error: {str(json_err)}")
                return "Error: Invalid response format from code analysis service"
                
        except requests.exceptions.Timeout:
            print(f"Timeout while analyzing {file_name}")
            return "Error: Analysis timeout - file may be too complex"
            
        except requests.exceptions.RequestException as e:
            print(f"Request error in analyze_code: {str(e)}")
            return f"Error analyzing code: Request failed"
            
    except Exception as e:
        print(f"Unexpected error in analyze_code: {str(e)}")
        print(f"Error type: {type(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return "Error: Unable to complete code analysis"

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
            return jsonify({'error': 'Invalid signature'}), 403

        event = request.headers.get('X-GitHub-Event')
        if event != 'pull_request':
            return jsonify({'status': 'skipped', 'reason': f'Event {event} not handled'}), 200

        payload = request.json
        action = payload['action']
        
        if action not in ['opened', 'synchronize']:
            return jsonify({'status': 'skipped', 'reason': f'Action {action} not handled'}), 200

        try:
            installation_id = payload['installation']['id']
            repo_name = payload['repository']['full_name']
            pr_number = payload['pull_request']['number']
            
            print(f"\n=== Processing PR #{pr_number} in {repo_name} ===")
            
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
            
            # Sort files by size and limit total files if needed
            files_to_review.sort(key=lambda x: len(x.patch) if x.patch else 0)
            if len(files_to_review) > 5:  # Limit number of files per review
                files_to_review = files_to_review[:5]
                review_header += "_Note: Only reviewing the 5 smallest changed files._\n\n"
            
            # Process one file at a time
            for file in files_to_review:
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
                
                # Ensure the comment isn't too long
                if len(combined_review) > 65000:
                    combined_review = combined_review[:65000] + "\n\n... (Review truncated due to length)"
                
                try:
                    pull.create_issue_comment(combined_review)
                    print("Successfully posted review")
                except Exception as e:
                    print(f"Error posting comment: {str(e)}")
                    # If we still can't post, try an even shorter version
                    try:
                        short_review = review_header + "\nReview too large to post. Summary of files reviewed:\n"
                        for file in files_to_review:
                            short_review += f"- {file.filename}\n"
                        pull.create_issue_comment(short_review)
                        print("Posted short review summary")
                    except Exception as e2:
                        print(f"Error posting short review: {str(e2)}")

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