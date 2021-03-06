from flask import Flask, render_template, request, make_response, redirect, jsonify
from flask_socketio import SocketIO, emit, send
from werkzeug.utils import secure_filename


from bs4 import BeautifulSoup as bs

import requests
import time
import json

from . import messaging

from .. import db
from .. import util
from .. import auth
from .. import space
from .. import variables
from .. import imagetype
from .. import exceptions

from bson import BSON 
from bson import json_util

# A variable that stores the socketio application from application.py for use in the /file endpoint
socket_app = None

@messaging.route("", methods = ['GET', 'POST'])
def show_chat():
    try:
        verified = auth.auth_credentials(request)
    except exceptions.AuthError:
        return render_template("index.html", error = 'Oops! Your password or your username was invalid!')
    
    cookies, username = verified 

    user = db.USERS_DB.userSecure.find_one({
        "username" : username
    })
        
    grade_page = requests.get("https://wa-bsd405-psv.edupoint.com/PXP_Gradebook.aspx?AGU=0", cookies = cookies).text
    grade_soup = bs(grade_page)

    # Check for an error, if there is one then the issue is LIKELY that the user's cookie was valid but expired
    # because of the way the error appears I don't think beautifulSoup catches it. So we're going to directly search for the error. 
    if "Object reference" in grade_page:
        return render_template("index.html", error = 'Your StudentVUE token has expired!')

    tables = util.get_info_tables(grade_soup, links = False)

    parsed_tables = util.parse_info_tables(tables)
    grade_table = parsed_tables[0]

    class_names = grade_table['Course Title']

    for c in class_names:
        classroom = db.CHAT_DB.classrooms.find_one({
            "class" : c
        })

        if not classroom:
            # add the class 
            db.CHAT_DB.classrooms.insert({
                "class" : c,
                "users" : {
                    username : {
                        "color" : "#4455ff"
                    }
                }
            })
        else:
            # check if the user is in the class
            if username not in classroom['users']:
                db.CHAT_DB.classrooms.update({
                    "_id" : classroom['_id']
                }, {
                    "$set" : {
                        "users." + username : {
                            "color" : "#4455ff"
                        }
                    }
                })
    
    token = db.USERS_DB.userSecure.find_one({
        "username" : username
    })['token']

    # Generate a token for each class that allows the user to verify he belongs to a class
    # The token is an HMAC derived from a secret serverside and their current cookie
    class_tokens = {c : auth.generate_class_token(c, token) for c in class_names}

    return render_template("chat_template.html", class_names = class_names, class_tokens = class_tokens, token = token, username = username, profile = user['settings']['profilePicture'])

@messaging.route("/recap", methods = ['POST'])
def get_recap():
    try:
        verified = auth.auth_credentials(request, method = 'COOKIES')
    except exceptions.AuthError:
        return jsonify(
            {
                "status" : "failure",
                "error" : "Invalid credentials"
            }
        ), 401
    
    _, username = verified 
    
    class_name = request.form.get("class")

    messages = db.CHAT_DB.messages.find({
        "classroom" : class_name
    }).sort([
        ("_id", 1)
        ]).limit(100)

    messages = [json.dumps(message, default = json_util.default) for message in messages]

    return jsonify(
        {
            'status' : "success",
            "messages" : messages
        }
    )

@messaging.route("/metadata", methods = ['POST'])
def get_metadata():
    try:
        verified = auth.auth_credentials(request, method = 'MESSAGING')
    except exceptions.AuthError:
        return jsonify(
            {
                "status" : "failure",
                "error" : "Invalid credentials"
            }
        ), 401
    
    _, username = verified 
    
    class_name = request.form.get("class")

    classroom = db.CHAT_DB.classrooms.find_one({
        "class" : class_name
    })

    # get colors
    colors = {user : classroom['users'][user]['color'] for user in classroom['users']}

    # get user profiles
    profiles = {}
    for username in classroom['users']:
        user = db.USERS_DB.userSecure.find_one({
            "username" : username
        })

        profiles[username] = user['settings']['profilePicture']

    return jsonify(
        {
            'status' : "success",
            "colors" : colors,
            "profiles" : profiles
        }
    )

@messaging.route("/file", methods = ['POST'])
def handle_file():
    try:
        verified = auth.auth_credentials(request, method = 'MESSAGING')
    except exceptions.AuthError: 
        return jsonify(
            {
                "status" : "failure",
                "error" : "Invalid Credentials"
            }
        ), 401

    _, username = verified 

    # check if there exists a file
    print(request.files)
    if "payload" not in request.files:
        return jsonify(
            {
                "status" : "failure",
                "error" : "Invalid Input"
            }
        ), 400

    f = request.files.get("payload")
    file_content = f.read()
    file_name = secure_filename(f.filename)
    file_extension = file_name.split(".")[-1]
    
    is_file_image = imagetype.what(file_content) in ['jpeg', 'png', 'gif']

    # Prevent potentially malicious files from being uploaded
    if (file_extension in variables.RESTRICTED_FILE_EXTENSIONS):
        return jsonify(
            {
                "status" : "failure",
                "error" : "Invalid Input"
            }
        )

    # Create the object and insert it into gradebook-space-1
    # Generate a random key name so that people can't access files
    key = util.salt(100)               
    key_name = request.form.get('class') + "/" + key
    key_url = ("https://gradebook-space-1.nyc3.digitaloceanspaces.com/" + key_name).replace(' ', '%20')     # Make the url web-friendly
    space.GRADEBOOK_SPACE.put_object(
        Body = file_content,
        Bucket = "gradebook-space-1",
        Key = request.form.get('class') + "/" + key,
        ACL='public-read',
        ContentDisposition = 'attachment; filename="%s"' % file_name
    )

    # insert a message with the url of the file, not of the file itself
    # type should also be replaced. If the file is an image, then type should be "image", otherwise it should be "file"
    db.CHAT_DB.messages.insert({
        "classroom" : request.form.get('class'),
        "content" : {
            "payload" : key_url,
            "type" : is_file_image * "image" + (not is_file_image) * "file",
            "file-name" : file_name
        },
        "timestamp" : int(time.time()),
        "author" : username
    }) 

    message = {
        "class" : request.form.get('class'),
        "type" : is_file_image * "image" + (not is_file_image) * "file",
        "file-name" : file_name,
        "timestamp" : int(time.time()),
        "payload" : key_url,
        "username" : username
    }

    socket_app.emit("message", message)

    return jsonify(
        {
            "status" : "success"
        }
    )

def init_application(application):
    @application.on('connect')
    def handle_connection():
        print("Connected to session ", request.sid)

        emit("connect", {"data" : "connect9ed"})

    @application.on("color_change")
    def handle_color_change(data):
        try:
            verified = auth.auth_credentials(data, method = 'SOCKET')
        except exceptions.AuthError as e:
            print(str(e))
            print('invalid credentials')
            return jsonify(
                {
                    "status" : "failure",
                    "error" : "Invalid credentials"
                }
            ), 401

        _, username = verified

        classroom = db.CHAT_DB.classrooms.find_one({
            "class" : data['class']
        })

        db.CHAT_DB.classrooms.update({
            "_id" : classroom['_id']
        }, {
            "$set" : {
                "users." + username + ".color" : data['payload']
            }
        })

        emit("color_change", {
            'username' : data['username'],
            'color' : data['payload']
        })

    @application.on("send")
    def handle_message(message):
        try:
            verified = auth.auth_credentials(message, method = 'SOCKET')
        except exceptions.AuthError as e:
            print(str(e))
            print('invalid credentials')
            return jsonify(
                {
                    "status" : "failure",
                    "error" : "Invalid credentials"
                }
            ), 401

        _, username = verified

        # TODO: Must be a way to verify validity of the classroom
        # AUTHENTICATION METHOD:
        # Store a random secret variable. When user connects to /messaging, generate a random salt. 
        # HMAC(SECRET, class_name + SALT) -> token
        # Send both the token and the salt to the user
        # When the user authenticates his classname, he will send both his salt, his class_name and his token
        # The salt and class_name are appended and HMACed with the SECRET, and if the result matches the sent token, we can ensure that the user belongs to that class.
        # If the user alters anything, he cannot find the corresponding token because he does not have the SECRET. 

        db.CHAT_DB.messages.insert({
            "classroom" : message['class'],
            "content" : {
                "payload" : message['payload'],
                "type" : message['type']
            },
            "timestamp" : int(time.time()),
            "author" : username
        })

        print('worked')

        # assign an id to the message and emit it
        # assign a timestamp to message as well
        message['timestamp'] = int(time.time())

        emit("ack", {"status" : "success"}, room = request.sid)
        emit("message", message, broadcast = True)
