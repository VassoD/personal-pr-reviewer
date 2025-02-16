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
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MISTRAL_API_KEY}"
    }
    
    system_prompt = """You are an expert software developer conducting code reviews.
    You will be shown a git patch/diff of code changes.
    Lines starting with '+' are additions and lines starting with '-' are deletions.
    ONLY review the specific changes shown in the diff - do not make assumptions about other parts of the code.
    Provide concise, actionable feedback focusing on code quality, best practices, and potential improvements.
    Format your review in clear sections for positive aspects and suggestions."""
    
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
        "max_tokens": 1000,
        "temperature": 0.7
    }
    
    try:
        print(f"Sending request to Mistral API for {file_name}")
        response = requests.post(MISTRAL_API_URL, headers=headers, json=data)
        response.raise_for_status()
        
        print(f"Successfully got response from Mistral API for {file_name}")
        review = response.json()["choices"][0]["message"]["content"]
        return review
    except Exception as e:
        print(f"Error in analyze_code: {str(e)}")
        print(f"Error type: {type(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return f"Error analyzing code: {str(e)}"

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
        print("Raw data:", request.data)
        print("JSON data:", request.json)
        print("Headers:", dict(request.headers))
        
        # Check environment variables
        print("\n=== Environment Variables ===")
        print("GITHUB_APP_ID exists:", bool(GITHUB_APP_ID))
        print("GITHUB_PRIVATE_KEY exists:", bool(GITHUB_PRIVATE_KEY))
        print("GITHUB_WEBHOOK_SECRET exists:", bool(GITHUB_WEBHOOK_SECRET))
        print("MISTRAL_API_KEY exists:", bool(MISTRAL_API_KEY))
        
        if not verify_webhook(request):
            print("Webhook verification failed")
            return jsonify({'error': 'Invalid signature'}), 403

        event = request.headers.get('X-GitHub-Event')
        print(f"\n=== Event Information ===")
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
            print(f"Installation ID: {installation_id}")
            
            print("\nGetting GitHub client...")
            gh = get_github_client(installation_id)
            print("Successfully got GitHub client")
            
            print("Getting repository...")
            repo = gh.get_repo(repo_name)
            print("Successfully got repository")
            
            print("Getting pull request...")
            pull = repo.get_pull(pr_number)
            print("Successfully got pull request")

            # Store all reviews to post a single combined comment
            reviews = []
            
            if action == 'opened':
                print("\n=== Processing New PR ===")
                files_to_review = pull.get_files()
                commits = list(pull.get_commits())
                commit_shas = [c.sha[:7] for c in commits]
                review_header = f"# Initial Code Review for PR\nReviewing all commits: {', '.join(commit_shas)}\n\n"
                print(f"Found {len(commit_shas)} commits to review")
            else:  # 'synchronize'
                print("\n=== Processing PR Update ===")
                last_review_time = get_last_review_timestamp(pull)
                print(f"Last review timestamp: {last_review_time}")
                
                all_commits = list(pull.get_commits())
                if last_review_time:
                    new_commits = [
                        commit for commit in all_commits
                        if commit.commit.author.date > last_review_time
                    ]
                    print(f"Found {len(new_commits)} new commits since last review")
                else:
                    new_commits = [all_commits[-1]]
                    print("No previous review found, reviewing latest commit")
                
                latest_files = get_files_from_commits(new_commits)
                print(f"Files changed in new commits: {latest_files}")
                
                files_to_review = [
                    file for file in pull.get_files()
                    if file.filename in latest_files
                ]
                
                commit_shas = [commit.sha[:7] for commit in new_commits]
                review_header = f"# Code Review for Latest Changes\nReviewing commits: {', '.join(commit_shas)}\n\n"
                
            print(f"\n=== Starting Reviews ===")
            print(f"Files to review: {len(files_to_review)}")
            
            # Review the files
            for file in files_to_review:
                try:
                    print(f"\nReviewing changes in {file.filename}")
                    
                    if file.patch:
                        print(f"Found patch for {file.filename}")
                        changes = "```diff\n" + file.patch + "\n```"
                    else:
                        print(f"No patch available for {file.filename}")
                        continue
                    
                    print(f"Analyzing code for {file.filename}")
                    review_comment = analyze_code(changes, file.filename)
                    print(f"Successfully analyzed {file.filename}")
                    reviews.append(f"### Review for `{file.filename}`:\n\n{review_comment}\n\n---\n\n")
                    
                except Exception as e:
                    print(f"Error processing {file.filename}: {str(e)}")
                    print(f"Error type: {type(e)}")
                    print(f"Traceback: {traceback.format_exc()}")
                    reviews.append(f"Error reviewing `{file.filename}`: {str(e)}\n\n---\n\n")

            if reviews:
                print("\n=== Posting Review ===")
                combined_review = review_header + "".join(reviews)
                print(f"Review length: {len(combined_review)} characters")
                try:
                    print("Attempting to post comment...")
                    result = pull.create_issue_comment(combined_review)
                    print("Successfully posted review comment")
                    print(f"Comment URL: {result.html_url if result else 'Unknown'}")
                except Exception as e:
                    print("Error posting comment:")
                    print(f"Error type: {type(e)}")
                    print(f"Error message: {str(e)}")
                    print(f"Traceback: {traceback.format_exc()}")
                    raise

            response = {'status': 'success'}
            print("\nFinal Response:", response)
            return jsonify(response), 200
            
        except Exception as e:
            print("\nError in GitHub operations:")
            print(f"Error type: {type(e)}")
            print(f"Error message: {str(e)}")
            print(f"Traceback: {traceback.format_exc()}")
            raise
            
    except Exception as e:
        error_msg = str(e)
        print("\nFatal Error:")
        print(f"Error type: {type(e)}")
        print(f"Error message: {error_msg}")
        print(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': error_msg}), 500

@app.route('/', methods=['GET'])
def home():
    return "PR Review Bot is running!"

if __name__ == '__main__':
    app.run(port=3000, debug=True)