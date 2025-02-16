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
        
    integration = GithubIntegration(
        GITHUB_APP_ID,
        GITHUB_PRIVATE_KEY.replace('\\n', '\n')  # Fix newline encoding
    )
    
    # Get an access token for the installation
    access_token = integration.get_access_token(installation_id).token
    return Github(access_token)

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
        response = requests.post(MISTRAL_API_URL, headers=headers, json=data)
        response.raise_for_status()
        
        review = response.json()["choices"][0]["message"]["content"]
        return review
    except Exception as e:
        print(f"Error in analyze_code: {str(e)}")
        return f"Error analyzing code: {str(e)}"

def get_last_review_timestamp(pull):
    """Get timestamp of the last review comment by the bot"""
    try:
        comments = pull.get_issue_comments()
        for comment in reversed(list(comments)):
            if comment.user.type == 'Bot' and ('Code Review for Latest Changes' in comment.body or 'Initial Code Review for PR' in comment.body):
                return comment.created_at
        return None
    except Exception as e:
        print(f"Error getting last review timestamp: {str(e)}")
        return None

def get_files_from_commits(commits):
    """Get all unique files changed in the given commits"""
    files_changed = set()
    for commit in commits:
        for file in commit.files:
            files_changed.add(file.filename)
    return files_changed

@app.route('/webhook', methods=['POST'])
def webhook():
    print("Received webhook")
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
        
        print(f"Processing PR #{pr_number} in {repo_name}")
        
        gh = get_github_client(installation_id)
        repo = gh.get_repo(repo_name)
        pull = repo.get_pull(pr_number)

        # Store all reviews to post a single combined comment
        reviews = []
        
        if action == 'opened':
            # For new PRs, review all files
            files_to_review = pull.get_files()
            commits = list(pull.get_commits())
            commit_shas = [c.sha[:7] for c in commits]
            review_header = f"# Initial Code Review for PR\nReviewing all commits: {', '.join(commit_shas)}\n\n"
        else:  # 'synchronize'
            # Get the timestamp of our last review
            last_review_time = get_last_review_timestamp(pull)
            
            # Get all commits since the last review
            all_commits = list(pull.get_commits())
            if last_review_time:
                new_commits = [
                    commit for commit in all_commits
                    if commit.commit.author.date > last_review_time
                ]
            else:
                # If we can't find the last review, just take the latest commit
                new_commits = [all_commits[-1]]
            
            # Get all files changed in new commits
            latest_files = get_files_from_commits(new_commits)
            
            # Filter PR files to only those changed in new commits
            files_to_review = [
                file for file in pull.get_files()
                if file.filename in latest_files
            ]
            
            # Create header with all new commit SHAs
            commit_shas = [commit.sha[:7] for commit in new_commits]
            review_header = f"# Code Review for Latest Changes\nReviewing commits: {', '.join(commit_shas)}\n\n"
            
        # Review the files
        for file in files_to_review:
            try:
                print(f"Reviewing changes in {file.filename}")
                
                if file.patch:
                    changes = "```diff\n" + file.patch + "\n```"
                else:
                    print(f"No patch available for {file.filename}")
                    continue
                
                review_comment = analyze_code(changes, file.filename)
                reviews.append(f"### Review for `{file.filename}`:\n\n{review_comment}\n\n---\n\n")
                
            except Exception as e:
                print(f"Error processing {file.filename}: {str(e)}")
                reviews.append(f"Error reviewing `{file.filename}`: {str(e)}\n\n---\n\n")

        if reviews:
            combined_review = review_header + "".join(reviews)
            print("Attempting to post review:", combined_review)
            try:
                pull.create_issue_comment(combined_review)
                print("Posted review comment")
            except Exception as e:
                print(f"Error posting comment: {str(e)}")

        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return "PR Review Bot is running!"

if __name__ == '__main__':
    app.run(port=3000, debug=True)